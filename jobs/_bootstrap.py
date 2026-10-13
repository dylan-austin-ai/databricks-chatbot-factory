"""Shared setup for job entry points: import path, Spark, settings, clients."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The bundle passes the deployed git commit, the environment (bundle target) and the SQL warehouse
# to every task (REL-7, REL-8). Settings read the environment from FACTORY_ENV.
for _arg in sys.argv:
    for _flag, _var in (("--git_commit=", "FACTORY_GIT_COMMIT"), ("--environment=", "FACTORY_ENV"),
                        ("--warehouse_id=", "FACTORY_WAREHOUSE_ID")):
        if _arg.startswith(_flag) and _arg.split("=", 1)[1]:
            os.environ[_var] = _arg.split("=", 1)[1]

from factory.config import PlatformSettings  # noqa: E402
from factory.controlplane import ControlPlane  # noqa: E402
from factory.sql import SparkRunner  # noqa: E402


def args(*names: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    for n in names:
        p.add_argument(f"--{n}", default="")
    return p.parse_known_args()[0]


def warehouse_for_traces(saved: str = "") -> str:
    """The SQL warehouse this job should use, made known to MLflow.

    The traces live in Unity Catalog, and MLflow reads and writes them through a SQL warehouse.
    Without one, anything that looks at traces (the quality check reading back its scores,
    production judging) stops with "SQL warehouse ID is required for accessing traces in UC
    tables". The app and the agent are given the id in their own settings; a job is a separate
    process and gets it from its `--warehouse_id` parameter, or, for a job copy made before that
    parameter existed, from the id saved in the platform settings (`saved`)."""
    warehouse = os.environ.get("FACTORY_WAREHOUSE_ID") or saved or ""
    if warehouse:
        os.environ["FACTORY_WAREHOUSE_ID"] = warehouse
        os.environ.setdefault("MLFLOW_TRACING_SQL_WAREHOUSE_ID", warehouse)
    return warehouse


def context(catalog: str = ""):
    from pyspark.sql import SparkSession
    from databricks.sdk import WorkspaceClient

    spark = SparkSession.builder.getOrCreate()
    sql = SparkRunner(spark)
    base = PlatformSettings.load({"catalog": catalog} if catalog else None)
    cp = ControlPlane(sql, base)
    try:
        overrides = cp.load_overrides()
    except Exception:  # first run, before the control plane exists
        overrides = None
    saved = str((overrides or {}).get("warehouse_id") or "")
    warehouse = warehouse_for_traces(saved)
    if overrides is not None and warehouse and warehouse != saved:
        try:  # remembered, so jobs that aren't passed the id (per-chatbot ingest copies) find it
            cp.set_setting("warehouse_id", warehouse, "system:job")
        except Exception as e:  # noqa: BLE001 - this job has it either way
            print(f"Warehouse id not saved to the settings: {type(e).__name__}")
    overrides = overrides or {}
    if warehouse:
        overrides["warehouse_id"] = warehouse
    if catalog:
        overrides["catalog"] = catalog
    settings = PlatformSettings.load(overrides)
    w = WorkspaceClient()
    cp = ControlPlane(sql, settings, w)  # republishes agent runtime files on every change
    return spark, sql, settings, cp, w


def vector_client():
    from databricks.vector_search.client import VectorSearchClient
    return VectorSearchClient(disable_notice=True)


ACTOR = os.environ.get("FACTORY_ACTOR", "system:job")


def uc_secret(settings, spec: str) -> str:
    """Read a Unity Catalog secret (<schema>.<name> in the platform catalog), governed by
    READ SECRET grants (SEC-4). Empty spec -> empty string."""
    if not spec:
        return ""
    from databricks.sdk.runtime import dbutils
    schema, key = spec.split(".", 1)
    return dbutils.secrets.get(catalog=settings.catalog, schema=schema, key=key)
