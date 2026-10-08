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
    sql = parse_binary_sql(p, 2)
    assert ":d0, :d1" in sql and "ai_parse_document" in sql and "pageRange" in sql
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
