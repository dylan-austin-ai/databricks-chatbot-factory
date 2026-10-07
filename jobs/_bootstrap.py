"""Shared setup for job entry points: import path, Spark, settings, clients."""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# The bundle passes the deployed git commit to every task (REL-7, REL-8).
for _arg in sys.argv:
    if _arg.startswith("--git_commit=") and _arg.split("=", 1)[1]:
        os.environ["FACTORY_GIT_COMMIT"] = _arg.split("=", 1)[1]

from factory.config import PlatformSettings  # noqa: E402
from factory.controlplane import ControlPlane  # noqa: E402
from factory.sql import SparkRunner  # noqa: E402


def args(*names: str) -> argparse.Namespace:
    p = argparse.ArgumentParser()
    for n in names:
        p.add_argument(f"--{n}", default="")
    return p.parse_known_args()[0]


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
        overrides = {}
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
