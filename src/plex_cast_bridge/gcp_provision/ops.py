"""Provisioning plan + optional live gcloud operations (admin machine ADC)."""

from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[3]
CLOUD_RUN_DIR = REPO_ROOT / "gcp-cloud-run"
CLOUD_RUN_ENV = CLOUD_RUN_DIR / ".env"
CLOUD_RUN_ENV_EXAMPLE = CLOUD_RUN_DIR / ".env.example"
DEPLOY_SCRIPT = CLOUD_RUN_DIR / "deploy.sh"


def _upsert_env_file(path: Path, updates: dict[str, str], example: Path | None = None) -> Path:
    """Merge key=value updates into an env file (create from example if missing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()
    elif example and example.exists():
        lines = example.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    seen: set[str] = set()
    out: list[str] = []
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            out.append(raw)
            continue
        key, _ = raw.split("=", 1)
        key = key.strip()
        if key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(raw)
    for key, value in updates.items():
        if key not in seen:
            out.append(f"{key}={value}")
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


def _read_env_value(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return None

REQUIRED_APIS = [
    "run.googleapis.com",
    "pubsub.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "firestore.googleapis.com",
    "storage.googleapis.com",
    "homegraph.googleapis.com",
]


@dataclass
class ProvisionPlan:
    project_id: str
    region: str = "us-central1"
    service_name: str = "plex-google-home-fulfillment"
    topic_id: str = "plex-commands"
    subscription_id: str = "plex-commands-local-worker"
    dlq_topic_id: str = "plex-commands-dlq"
    service_account: str = "plex-fulfillment-sa"
    create_project: bool = False
    project_name: str = "Plex Cast Bridge"
    steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_provision_plan(
    *,
    project_id: str,
    create_project: bool = False,
    region: str = "us-central1",
    recreate_canonical: bool = False,
) -> ProvisionPlan:
    if recreate_canonical:
        project_id = "plex-cast-bridge-home"
        create_project = False
    steps = []
    if create_project:
        steps.append("create_project")
    steps.extend(
        [
            "confirm_billing",
            "enable_apis",
            "ensure_firestore",
            "ensure_pubsub",
            "ensure_service_account",
            "ensure_catalog_bucket",
            "deploy_cloud_run",
            "mint_local_secrets",
        ]
    )
    plan = ProvisionPlan(
        project_id=project_id,
        region=region,
        create_project=create_project,
        steps=steps,
    )
    return plan


def auth_status() -> dict[str, Any]:
    """Report gcloud login + ADC availability on this machine."""
    account = ""
    try:
        proc = subprocess.run(
            ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"],
            capture_output=True,
            text=True,
            check=False,
        )
        account = (proc.stdout or "").strip().splitlines()[0] if proc.stdout.strip() else ""
    except Exception as exc:
        return {"ok": False, "account": None, "adc": False, "detail": str(exc)}
    adc_ok = (
        subprocess.run(
            ["gcloud", "auth", "application-default", "print-access-token"],
            capture_output=True,
            text=True,
            check=False,
        ).returncode
        == 0
    )
    return {
        "ok": bool(account) and adc_ok,
        "account": account or None,
        "adc": adc_ok,
        "detail": "ready" if account and adc_ok else "Run gcloud auth login && gcloud auth application-default login",
    }


def verify_project_resources(project_id: str, region: str = "us-central1") -> dict[str, Any]:
    """Check whether canonical resources exist in a project."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str):
        checks.append({"name": name, "ok": ok, "detail": detail})

    desc = subprocess.run(
        ["gcloud", "projects", "describe", project_id, "--format=value(lifecycleState)"],
        capture_output=True,
        text=True,
        check=False,
    )
    add("project", desc.returncode == 0 and "ACTIVE" in (desc.stdout or ""), (desc.stdout or desc.stderr or "").strip())

    enabled = subprocess.run(
        ["gcloud", "services", "list", "--enabled", f"--project={project_id}", "--format=value(config.name)"],
        capture_output=True,
        text=True,
        check=False,
    )
    enabled_set = set((enabled.stdout or "").splitlines())
    missing = [a for a in REQUIRED_APIS if a not in enabled_set]
    add("apis", not missing, "ok" if not missing else f"missing: {', '.join(missing)}")

    topics = subprocess.run(
        ["gcloud", "pubsub", "topics", "list", f"--project={project_id}", "--format=value(name)"],
        capture_output=True,
        text=True,
        check=False,
    )
    topic_ok = any(n.endswith("/topics/plex-commands") for n in (topics.stdout or "").splitlines())
    add("pubsub_topic", topic_ok, "plex-commands" if topic_ok else "missing plex-commands")

    subs = subprocess.run(
        ["gcloud", "pubsub", "subscriptions", "list", f"--project={project_id}", "--format=value(name)"],
        capture_output=True,
        text=True,
        check=False,
    )
    sub_ok = any(n.endswith("/subscriptions/plex-commands-local-worker") for n in (subs.stdout or "").splitlines())
    add("pubsub_subscription", sub_ok, "plex-commands-local-worker" if sub_ok else "missing subscription")

    sa = subprocess.run(
        [
            "gcloud",
            "iam",
            "service-accounts",
            "describe",
            f"plex-fulfillment-sa@{project_id}.iam.gserviceaccount.com",
            f"--project={project_id}",
            "--format=value(email)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    add("fulfillment_sa", sa.returncode == 0, (sa.stdout or "").strip() or "missing")

    run = subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            "plex-google-home-fulfillment",
            f"--project={project_id}",
            f"--region={region}",
            "--format=value(status.url)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    url = (run.stdout or "").strip()
    add("cloud_run", run.returncode == 0 and bool(url), url or "not deployed")

    return {
        "project_id": project_id,
        "ok": all(c["ok"] for c in checks if c["name"] != "cloud_run"),
        "cloud_run_ready": any(c["name"] == "cloud_run" and c["ok"] for c in checks),
        "cloud_run_url": url or None,
        "checks": checks,
    }


def describe_cost_notes(plan: ProvisionPlan) -> list[str]:
    return [
        "Cloud Run charges for request time after the free tier.",
        "Pub/Sub has a free monthly allowance; beyond that messages are billed.",
        "Cloud Build minutes used for source deploys may incur charges.",
        "Firestore and GCS free tiers usually cover personal catalog sizes.",
        "HomeGraph API calls are typically free for personal sync volumes.",
        f"Target project: {plan.project_id} (region {plan.region}).",
    ]


_GCLOUD_ENV = {
    **os.environ,
    "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
    "CLOUDSDK_CORE_VERBOSITY": "error",
}


def _gcloud(args: list[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    cmd = ["gcloud", *args]
    if "--format" not in args and "--format=json" not in " ".join(args) and not any(a.startswith("--format=") for a in args):
        cmd.append("--format=json")
    if "--quiet" not in args and "-q" not in args:
        cmd.insert(1, "--quiet")
    log.info("Running: %s", " ".join(c for c in cmd if c != "--format=json"))
    return subprocess.run(cmd, check=check, capture_output=True, text=True, env=_GCLOUD_ENV)


def _err_text(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stderr or proc.stdout or "").strip()[:500]


def run_provision_step(plan: ProvisionPlan, step: str, dry_run: bool = True) -> dict[str, Any]:
    """Execute one idempotent provision step. dry_run=True never mutates cloud."""
    if dry_run:
        return {"step": step, "status": "preview", "detail": would_run(step, plan)}

    try:
        return _run_provision_step_inner(plan, step)
    except Exception as exc:
        log.exception("provision step %s failed", step)
        return {"step": step, "status": "failed", "detail": str(exc)[:500]}


def _run_provision_step_inner(plan: ProvisionPlan, step: str) -> dict[str, Any]:
    if step == "create_project":
        existing = _gcloud(["projects", "describe", plan.project_id, "--format=value(projectId)"])
        if existing.returncode == 0 and (existing.stdout or "").strip():
            return {"step": step, "status": "exists", "project_id": plan.project_id}
        created = _gcloud(
            [
                "projects",
                "create",
                plan.project_id,
                f"--name={plan.project_name}",
                "--set-as-default",
            ]
        )
        if created.returncode != 0:
            return {"step": step, "status": "failed", "detail": _err_text(created)}
        return {"step": step, "status": "created", "project_id": plan.project_id}

    if step == "confirm_billing":
        billing_url = f"https://console.cloud.google.com/billing/linkedaccount?project={plan.project_id}"
        # Query against this project as the billing/quota project to avoid interactive prompts
        # against an unrelated consumer project.
        _gcloud(
            [
                "services",
                "enable",
                "cloudbilling.googleapis.com",
                f"--project={plan.project_id}",
                f"--billing-project={plan.project_id}",
            ]
        )
        billing = _gcloud(
            [
                "billing",
                "projects",
                "describe",
                plan.project_id,
                "--format=json",
                f"--billing-project={plan.project_id}",
            ]
        )
        if billing.returncode == 0:
            try:
                payload = json.loads(billing.stdout or "{}")
            except Exception:
                payload = {}
            account = payload.get("billingAccountName") or payload.get("billingAccountId")
            enabled = bool(payload.get("billingEnabled"))
            if enabled and account:
                return {
                    "step": step,
                    "status": "ok",
                    "detail": f"Billing linked ({account})",
                    "url": billing_url,
                }
            return {
                "step": step,
                "status": "needs_user",
                "detail": "Billing is not linked to this project yet. Link an open billing account, then re-run provision.",
                "url": billing_url,
                "billing_enabled": False,
            }
        return {
            "step": step,
            "status": "needs_user",
            "detail": (
                "Could not read billing status. Open the billing URL, link an account, then re-run provision."
            ),
            "url": billing_url,
            "raw": _err_text(billing),
        }
    if step == "link_billing":
        billing_url = f"https://console.cloud.google.com/billing/linkedaccount?project={plan.project_id}"
        accounts = _gcloud(
            ["billing", "accounts", "list", "--format=json", f"--billing-project={plan.project_id}"]
        )
        if accounts.returncode != 0:
            return {"step": step, "status": "failed", "detail": _err_text(accounts), "url": billing_url}
        try:
            rows = json.loads(accounts.stdout or "[]")
        except Exception:
            rows = []
        open_accounts = [a for a in rows if a.get("open")]
        if not open_accounts:
            return {
                "step": step,
                "status": "needs_user",
                "detail": "No open billing accounts found for this Google account.",
                "url": billing_url,
            }
        account_name = open_accounts[0]["name"]  # billingAccounts/XXXX
        linked = _gcloud(
            [
                "billing",
                "projects",
                "link",
                plan.project_id,
                f"--billing-account={account_name}",
                f"--billing-project={plan.project_id}",
            ]
        )
        if linked.returncode != 0:
            return {"step": step, "status": "failed", "detail": _err_text(linked), "url": billing_url}
        return {
            "step": step,
            "status": "ok",
            "detail": f"Linked {account_name}",
            "billing_account": account_name,
            "url": billing_url,
        }
    if step == "enable_apis":
        # Billing must be linked before most APIs enable.
        billing = run_provision_step(plan, "confirm_billing", dry_run=False)
        if billing.get("status") != "ok":
            return {
                "step": step,
                "status": "failed",
                "detail": billing.get("detail") or "Link billing before enabling APIs",
                "url": billing.get("url"),
            }
        failed: list[str] = []
        for api in REQUIRED_APIS:
            proc = _gcloud(["services", "enable", api, f"--project={plan.project_id}"])
            if proc.returncode != 0:
                failed.append(f"{api}: {_err_text(proc)}")
        if failed:
            return {"step": step, "status": "failed", "detail": "; ".join(failed)[:800], "url": billing.get("url")}
        return {"step": step, "status": "ok", "apis": REQUIRED_APIS}
    if step == "ensure_firestore":
        proc = _gcloud(
            [
                "firestore",
                "databases",
                "describe",
                f"--project={plan.project_id}",
                "--database=(default)",
            ]
        )
        if proc.returncode != 0:
            created = _gcloud(
                [
                    "firestore",
                    "databases",
                    "create",
                    "--location=" + plan.region,
                    f"--project={plan.project_id}",
                    "--database=(default)",
                ]
            )
            if created.returncode != 0:
                return {"step": step, "status": "failed", "detail": _err_text(created)}
            return {"step": step, "status": "created"}
        return {"step": step, "status": "exists"}
    if step == "ensure_pubsub":
        for topic in (plan.topic_id, plan.dlq_topic_id):
            _gcloud(["pubsub", "topics", "create", topic, f"--project={plan.project_id}"])
        _gcloud(
            [
                "pubsub",
                "subscriptions",
                "create",
                plan.subscription_id,
                f"--topic={plan.topic_id}",
                f"--project={plan.project_id}",
                f"--dead-letter-topic={plan.dlq_topic_id}",
                "--max-delivery-attempts=5",
            ]
        )
        return {"step": step, "status": "ok"}
    if step == "ensure_service_account":
        email = f"{plan.service_account}@{plan.project_id}.iam.gserviceaccount.com"
        _gcloud(
            [
                "iam",
                "service-accounts",
                "create",
                plan.service_account,
                "--display-name=Plex Google Home Fulfillment SA",
                f"--project={plan.project_id}",
            ]
        )
        for role in (
            "roles/pubsub.publisher",
            "roles/datastore.user",
            "roles/storage.objectAdmin",
        ):
            _gcloud(
                [
                    "projects",
                    "add-iam-policy-binding",
                    plan.project_id,
                    f"--member=serviceAccount:{email}",
                    f"--role={role}",
                    "--condition=None",
                ]
            )
        return {"step": step, "status": "ok", "email": email}
    if step == "ensure_catalog_bucket":
        bucket = f"{plan.project_id}-catalog"
        _gcloud(
            [
                "storage",
                "buckets",
                "create",
                f"gs://{bucket}",
                f"--project={plan.project_id}",
                f"--location={plan.region}",
            ]
        )
        return {"step": step, "status": "ok", "bucket": bucket}
    if step == "deploy_cloud_run":
        if not DEPLOY_SCRIPT.exists():
            return {
                "step": step,
                "status": "failed",
                "detail": f"Missing deploy script at {DEPLOY_SCRIPT}",
            }
        required = (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "ADMIN_PASSPHRASE",
            "OAUTH_SIGNING_SECRET",
            "CATALOG_WRITE_KEY",
        )
        missing = [k for k in required if not _read_env_value(CLOUD_RUN_ENV, k)]
        if missing:
            return {
                "step": step,
                "status": "failed",
                "detail": f"Mint secrets first; missing in gcp-cloud-run/.env: {', '.join(missing)}",
            }
        # Keep project id aligned with the attached wizard project.
        _upsert_env_file(
            CLOUD_RUN_ENV,
            {
                "GCP_PROJECT_ID": plan.project_id,
                "GCP_PUBSUB_TOPIC_ID": plan.topic_id,
                "USE_CLOUD_CATALOG": "true",
                "CATALOG_BUCKET": f"{plan.project_id}-catalog",
                "ENVIRONMENT": "production",
                "MOCK_PUBSUB": "false",
            },
            example=CLOUD_RUN_ENV_EXAMPLE,
        )
        proc = subprocess.run(
            ["bash", str(DEPLOY_SCRIPT), plan.project_id, plan.region],
            cwd=str(CLOUD_RUN_DIR),
            capture_output=True,
            text=True,
            check=False,
            timeout=1800,
        )
        url_proc = subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                plan.service_name,
                f"--project={plan.project_id}",
                f"--region={plan.region}",
                "--format=value(status.url)",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        url = (url_proc.stdout or "").strip()
        if proc.returncode != 0 or not url:
            detail = (proc.stderr or proc.stdout or url_proc.stderr or "deploy failed").strip()
            return {
                "step": step,
                "status": "failed",
                "detail": detail[-2000:],
                "cloud_run_url": url or None,
            }
        return {
            "step": step,
            "status": "ok",
            "cloud_run_url": url,
            "detail": f"Deployed {plan.service_name}",
        }
    if step == "mint_local_secrets":
        generated = {
            "GOOGLE_CLIENT_ID": f"plex-gh-{secrets.token_hex(4)}",
            "GOOGLE_CLIENT_SECRET": secrets.token_urlsafe(32),
            "ADMIN_PASSPHRASE": secrets.token_urlsafe(18),
            "OAUTH_SIGNING_SECRET": secrets.token_urlsafe(48),
            "CATALOG_WRITE_KEY": secrets.token_urlsafe(32),
            "WEBHOOK_API_KEY": secrets.token_urlsafe(24),
        }
        env_updates = {
            **generated,
            "GCP_PROJECT_ID": plan.project_id,
            "GCP_PUBSUB_TOPIC_ID": plan.topic_id,
            "USE_CLOUD_CATALOG": "true",
            "CATALOG_BUCKET": f"{plan.project_id}-catalog",
            "ENVIRONMENT": "production",
            "MOCK_PUBSUB": "false",
        }
        _upsert_env_file(CLOUD_RUN_ENV, env_updates, example=CLOUD_RUN_ENV_EXAMPLE)
        return {
            "step": step,
            "status": "ok",
            "generated": generated,
            "env_file": str(CLOUD_RUN_ENV),
            "warning": "Secrets written to gcp-cloud-run/.env on this Mac; keep that file private.",
        }
    return {"step": step, "status": "unknown"}


def would_run(step: str, plan: ProvisionPlan) -> str:
    return f"Would run step '{step}' on project {plan.project_id} in {plan.region}"
