import pytest

from factory.controlplane import ControlPlane, render
from factory.ingestion import BotPaths, approved_chunks_select, chunk_sql, parse_binary_sql
from factory.provisioning import Provisioner, principal
from factory.sql import ident


def test_ident_rejects_injection():
    assert ident("cat", "claims_chatbot") == "`cat`.`claims_chatbot`"
    with pytest.raises(ValueError):
        ident("cat", "x`; DROP TABLE y; --")


def test_principal_quoting():
    assert principal("claims team") == "`claims team`"
    with pytest.raises(ValueError):
        principal("bad`name")


def test_ddl_renders(settings):
    stmts = render("controlplane_ddl.sql", catalog="`c`", platform="`_platform`",
                   tag_chatbot="chatbot_name", shared_value="shared-platform")
    assert any("CREATE TABLE IF NOT EXISTS `c`.`_platform`.bots" in s for s in stmts)
    bot = render("bot_schema_ddl.sql", catalog="`c`", schema="`b`", display_name="B")
    assert any("CREATE VOLUME IF NOT EXISTS `c`.`b`.docs" in s for s in bot)
    views = render("dashboard_views.sql", catalog="`c`", platform="`_platform`",
                   tag_chatbot="chatbot_name", shared_value="shared-platform",
                   environment="qa", workspace_id="123")
    assert any("v_observability_daily" in v for v in views)
    assert any("v_cost_by_tag" in v and "u.workspace_id = '123'" in v for v in views)
    gov = render("governance.sql", catalog="`c`", platform="`p`", security="`security`", admins="`mlops`", extra=", `app-sp`")
    assert any("CREATE OR REPLACE POLICY mask_pii_text" in g and "EXCEPT `security`, `mlops`" in g for g in gov)
    assert any("ai_mask(" in g for g in gov)
    assert len(render("metric_views.sql", catalog="`c`", platform="`p`")) == 2
    system = render("system_views.sql", catalog="`c`", platform="`p`", tag_chatbot="chatbot_name",
                    environment="qa", workspace_id="123")
    assert any("request_tags['environment'] = 'qa'" in v for v in system)
    assert any("system.query.history" in v and "workspace_id = '123'" in v for v in system)


def test_pipeline_sql_shapes(settings):
    p = BotPaths(settings, "claims_chatbot")
    sql = parse_binary_sql(p, "abc123", 2, f"{p.files}/abc123_v2.pdf", "1-3, 5")
    assert "ai_parse_document" in sql and "'pageRange', '1-3,5'" in sql
    delete, insert, count = chunk_sql(p, settings, 1)
    assert "ai_prep_search(text_content" in insert and "ai_prep_search(parsed" in insert
    assert "doc_type" in insert and "'schema'" in insert
    sel = approved_chunks_select(p)
    assert "m.status = 'approved' AND m.is_active" in sel
    assert "expires_ts" in sel and "effective_ts" in sel and "'claims_chatbot' AS bot_id" in sel


def test_provisioning_is_idempotent(settings, cfg, fake_sql):
    fake_sql.answers = [(r"FROM .*provisioning_steps", [{"step": "create_objects"},
                                                         {"step": "tag_objects"}])]
    cp = ControlPlane(fake_sql, settings)
    Provisioner(fake_sql, settings, cp).run(cfg, "tester", only=["create_objects", "tag_objects",
                                                                  "grant_access"])
    assert not fake_sql.find(r"CREATE SCHEMA")            # already done -> skipped
    grants = fake_sql.find(r"^GRANT")
    assert any("`claims-adjusters`" in g and "access_probe" in g for g in grants)
    assert not any("MODIFY" in g for g in grants)           # users never get MODIFY (LCY-5)
    assert not any("`claims-adjusters`" in g and "manifest" in g for g in grants)


def test_middleware_bot_grants_no_end_users(settings, cfg, fake_sql):
    cfg.access_mode, cfg.sensitivity = "middleware", "public"
    Provisioner(fake_sql, settings, ControlPlane(fake_sql, settings)).grant_access(cfg)
    assert not any("claims-adjusters" in g for g in fake_sql.find(r"^GRANT"))


def test_tags_applied(settings, cfg, fake_sql):
    Provisioner(fake_sql, settings, ControlPlane(fake_sql, settings)).tag_objects(cfg)
    tagged = fake_sql.find("SET TAGS")
    assert all("'chatbot_name' = 'claims_chatbot'" in t for t in tagged)
    assert any("ALTER SCHEMA" in t for t in tagged)


def test_split_ignores_semicolons_in_strings_and_comments():
    from factory.controlplane import split_statements

    text = """
    -- header comment; with a semicolon
    CREATE SCHEMA s COMMENT 'Managed by the app; no manual edits';
    CREATE TABLE t (
      a INT, b INT,   -- first; second
      c STRING        /* block; comment */
    );
    SELECT 'it''s; fine', "double; quoted", `odd;name`, 'back\\'slash; too' FROM t;
    SELECT 1
    """
    stmts = split_statements(text)
    assert len(stmts) == 4
    assert stmts[0] == "CREATE SCHEMA s COMMENT 'Managed by the app; no manual edits'"
    assert stmts[1].startswith("CREATE TABLE t (") and stmts[1].endswith(")")
    assert "first; second" not in stmts[1] and "c STRING" in stmts[1]
    assert "'it''s; fine'" in stmts[2] and '"double; quoted"' in stmts[2] and "`odd;name`" in stmts[2]
    assert stmts[3] == "SELECT 1"


def test_string_that_looks_like_a_comment_is_kept():
    from factory.controlplane import split_statements

    assert split_statements("SELECT '-- not a comment; really' AS x;") == ["SELECT '-- not a comment; really' AS x"]


def test_every_rendered_statement_is_whole(settings):
    import re

    values = dict(catalog="`c`", platform="`_platform`", schema="`b`", display_name="B",
                  tag_chatbot="chatbot_name", shared_value="shared-platform",
                  security="`security`", admins="`mlops`", extra="", environment="qa", workspace_id="123")
    starts = re.compile(r"^(CREATE|ALTER|GRANT|REVOKE|DROP|INSERT|MERGE|COMMENT|SET|WITH|SELECT|REFRESH)\b", re.I)
    for name in ("controlplane_ddl.sql", "bot_schema_ddl.sql", "dashboard_views.sql", "governance.sql",
                 "system_views.sql", "metric_views.sql"):
        for stmt in render(name, **values):
            assert starts.match(stmt), f"{name}: fragment {stmt[:60]!r}"
            assert stmt.count("(") == stmt.count(")"), f"{name}: unbalanced {stmt[:60]!r}"
    schema = render("controlplane_ddl.sql", **values)[0]
    assert schema.endswith("Managed by the app; no manual edits (LCY-5, REL-10).'")


def test_platform_release_insert_uses_select_and_a_generated_id(settings, fake_sql):
    import json
    import uuid

    from factory.controlplane import ControlPlane

    rid = ControlPlane(fake_sql, settings).record_platform_release(
        "1.0.0", "abc123", "qa", "7", True, "deployer@corp.com", git_branch="main",
        git_origin="git@github.com:org/repo.git", config={"catalog": "chatbots_test", "endpoint": "chatbot-agent"})
    (stmt, params), = fake_sql.statements
    assert stmt.startswith("INSERT INTO chatbots_test._platform.platform_releases (release_id, platform_version,")
    assert " SELECT :id, :pv, :gc, :env, :mv, CAST(:gate AS BOOLEAN), :who, current_timestamp(), " in stmt
    assert "VALUES" not in stmt and "uuid()" not in stmt
    assert uuid.UUID(rid) and params["id"] == rid
    assert (params["gc"], params["env"], params["who"], params["branch"]) == ("abc123", "qa", "deployer@corp.com", "main")
    config = json.loads(params["config"])
    assert config["catalog"] == "chatbots_test" and len(config["settings_sha256"]) == 64


def test_control_plane_adds_columns_missing_from_existing_tables(settings):
    from conftest import FakeSql
    from factory.controlplane import ControlPlane

    old_table = [{"col_name": c} for c in ("release_id", "platform_version", "git_commit", "environment",
                                           "model_version", "gate_enforced", "deployed_by", "ts", "git_branch")]
    sql = FakeSql(answers=[(r"DESCRIBE TABLE .*platform_releases", old_table)])
    ControlPlane(sql, settings).ensure()
    alters = sql.find(r"^ALTER TABLE chatbots_test\._platform\.platform_releases ADD COLUMNS")
    assert alters == ["ALTER TABLE chatbots_test._platform.platform_releases ADD COLUMNS "
                      "(git_origin STRING, config_json STRING)"]

    current = FakeSql(answers=[(r"DESCRIBE TABLE", old_table + [{"col_name": "git_origin"}, {"col_name": "config_json"}])])
    ControlPlane(current, settings).ensure()
    assert current.find(r"ADD COLUMNS") == []


def test_no_sql_generates_ids_with_uuid_function():
    """Databricks SQL rejects uuid() in a parameterized VALUES clause; ids are generated in Python."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    files = [f for d in ("src", "app", "agent", "jobs", "sql") for f in (root / d).rglob("*")
             if f.suffix in (".py", ".sql")]
    assert files
    offenders = [str(f.relative_to(root)) for f in files if "uuid()" in f.read_text()]
    assert offenders == []


def test_metric_view_yaml_parses_with_backticked_names():
    import yaml

    stmts = render("metric_views.sql", catalog="`qa_chatbot_factory`", platform="`_platform`")
    assert len(stmts) == 2
    sources = []
    for stmt in stmts:
        head, body, tail = stmt.split("$$")
        assert head.startswith("CREATE OR REPLACE VIEW `qa_chatbot_factory`.`_platform`.mv_chatbot_")
        assert tail.strip() == ""
        spec = yaml.safe_load(body)
        assert spec["version"] == 1.1 and spec["fields"] and spec["measures"]
        assert all(set(item) == {"name", "expr"} for item in spec["fields"] + spec["measures"])
        sources.append(spec["source"])
    assert sources == ["`qa_chatbot_factory`.`_platform`.request_log",
                       "`qa_chatbot_factory`.`_platform`.judge_results"]


def test_documents_page_only_reads_columns_the_manifest_has():
    """The page indexes manifest rows by column name; a name the table lacks is a KeyError."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    ddl = (root / "sql" / "bot_schema_ddl.sql").read_text()
    manifest = ddl[ddl.index(".manifest ("):]
    manifest = manifest[:manifest.index("TBLPROPERTIES")]
    columns = set(re.findall(r"^\s+([a-z_]+) [A-Z]+", manifest, re.M))
    assert {"doc_id", "doc_name", "status", "no_expiry"} <= columns
    page = (root / "app" / "pages" / "documents.py").read_text()
    used = set(re.findall(r"""(?:doc|names\[d\]|r)\[["']([a-z_]+)["']\]""", page))
    used -= {"bot_id", "display_name"}          # chatbot rows, not manifest rows
    assert used and used - columns == set(), f"not in the manifest table: {sorted(used - columns)}"


def test_parse_statement_has_only_constant_options(settings):
    """ai_parse_document rejects an option map built from columns, so every value is a literal."""
    import re

    p = BotPaths(settings, "claims_chatbot")
    sql = parse_binary_sql(p, "abc123", 3, f"{p.files}/abc123_v3.pdf")
    options = re.search(r"ai_parse_document\(f\.content, map\((.*?)\)\)", sql, re.S).group(1)
    assert "||" not in options and "m." not in options and ":" not in options.replace("'2.0'", "")
    assert f"'imageOutputPath', '{p.images}/abc123/v3/'" in options       # per-document image folder kept
    assert "pageRange" not in options
    assert f"read_files('{p.files}/abc123_v3.pdf', format => 'binaryFile')" in sql   # reads only this file
    assert "SELECT :d, CAST(:v AS INT), 'ai_parse_document'" in sql

    ranged = parse_binary_sql(p, "abc123", 3, f"{p.files}/abc123_v3.pdf", "2-4,9")
    assert "'pageRange', '2-4,9'" in ranged


def test_parse_statement_refuses_unsafe_values(settings):
    import pytest

    p = BotPaths(settings, "claims_chatbot")
    good = f"{p.files}/abc123_v1.pdf"
    for doc_id, path, pages in [
        ("abc'); DROP TABLE x; --", good, None),
        ("abc123", "/Volumes/other/bot/files/x.pdf", None),            # another chatbot's folder
        ("abc123", f"{p.files}/../../secret.pdf", None),
        ("abc123", f"{p.files}/a' || b.pdf", None),
        ("abc123", good, "1-3'); DROP TABLE x; --"),
        ("abc123", good, "all"),
    ]:
        with pytest.raises(ValueError):
            parse_binary_sql(p, doc_id, 1, path, pages)
    with pytest.raises((ValueError, TypeError)):
        parse_binary_sql(p, "abc123", "1; DROP", good)


def test_sql_literal_escapes_quotes_and_backslashes():
    from factory.ingestion import sql_literal

    assert sql_literal("plain") == "'plain'"
    assert sql_literal("it's") == "'it\\'s'"
    assert sql_literal("a\\b") == "'a\\\\b'"


def test_alerts_use_fields_the_sdk_has():
    """AlertV2 has custom_summary and custom_description; there is no custom_subject."""
    import pathlib

    from databricks.sdk.service import sql as dsql

    fields = set(dsql.AlertV2.__dataclass_fields__)
    assert {"custom_summary", "custom_description"} <= fields and "custom_subject" not in fields
    source = (pathlib.Path(__file__).resolve().parents[1] / "src" / "factory" / "alerting.py").read_text()
    assert "custom_subject" not in source and "custom_summary=" in source


def test_alert_objects_build_with_the_installed_sdk(settings, cfg):
    """Constructing the alerts with real SDK classes catches any argument the SDK doesn't accept."""
    from types import SimpleNamespace

    from factory.alerting import ensure_bot_alerts

    created = []
    w = SimpleNamespace(alerts_v2=SimpleNamespace(list_alerts=lambda: [], create_alert=created.append,
                                                  update_alert=lambda *a, **k: None))
    names = ensure_bot_alerts(w, settings, cfg, "wh1")
    assert len(created) == 2 and names == [a.display_name for a in created]
    first = created[0]
    assert first.custom_summary == "Chatbot alert: claims_chatbot" and first.custom_description
    assert first.warehouse_id == "wh1" and first.evaluation.source.name == "n"
    assert first.as_dict()["custom_summary"] == "Chatbot alert: claims_chatbot"     # serializes for the API


def test_pipeline_parses_one_document_version_per_statement(settings, cfg):
    from conftest import FakeSql
    from factory.pipeline import Pipeline

    p = BotPaths(settings, cfg.bot_id)
    targets = [
        {"doc_id": "aaa111", "doc_version": 1, "parser": "ai_parse_document", "page_count": 3, "doc_name": "A.pdf",
         "file_path": f"{p.files}/aaa111_v1.pdf", "page_range": None},
        {"doc_id": "bbb222", "doc_version": 4, "parser": "ai_parse_document", "page_count": 9, "doc_name": "B.pdf",
         "file_path": f"{p.files}/bbb222_v4.pdf", "page_range": "1-2"},
        {"doc_id": "ccc333", "doc_version": 1, "parser": "text_reader", "page_count": None, "doc_name": "C.md",
         "file_path": f"{p.files}/ccc333_v1.md", "page_range": None},
    ]
    sql = FakeSql(answers=[(r"LEFT ANTI JOIN", targets)])
    pipe = Pipeline.__new__(Pipeline)
    pipe.sql, pipe.s, pipe.log = sql, settings, lambda m: None
    for name in ("injection_scan", "extraction_qa", "generate_golden"):
        setattr(pipe, name, lambda *a, **k: {})
    pipe.publish = lambda cfg, actor: "r1"
    cfg.golden_set_mode = "skip"
    pipe.run(cfg)
    parses = [(s, prm) for s, prm in sql.statements if "ai_parse_document(f.content" in s]
    assert [prm for _, prm in parses] == [{"d": "aaa111", "v": 1}, {"d": "bbb222", "v": 4}]
    assert f"'{p.images}/aaa111/v1/'" in parses[0][0] and "pageRange" not in parses[0][0]
    assert f"'{p.images}/bbb222/v4/'" in parses[1][0] and "'pageRange', '1-2'" in parses[1][0]
    assert len([s for s, _ in sql.statements if "'text_reader'" in s and "INSERT INTO" in s]) == 1
