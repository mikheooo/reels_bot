import asyncio
import logging
import os
import random
import re
import shutil
import time
import traceback
import uuid
from datetime import datetime, timezone

import google.generativeai as genai
import googleapiclient.http
from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import FSInputFile
from sqlalchemy import select

from app.bot.analysis_view import analysis_keyboard
from app.core.config import settings
from app.core.normalizer import clean_url
from app.db.database import AsyncSessionLocal
from app.db.models import (
    ContentDeliveryModel,
    ContentPackageModel,
    Job,
    PublicationIntentModel,
    Task,
)
from app.worker.business_check import format_business_check_markdown, run_business_check
from app.worker.compact_renderer import build_detail_sections, render_compact_analysis
from app.worker.content_package import (
    VALID_TRANSITIONS,
    ContentPackage,
    DeliveryOutcome,
    DeliveryRecord,
    PackageStatus,
    PublicationIntent,
    PublicationIntentStatus,
    TargetDeliveryStatus,
    TargetPlatform,
    build_content_package,
    compute_payload_hash,
    reconcile_package_status,
    transition_package_status,
)
from app.worker.content_router import fallback_route, route_content
from app.worker.factcheck import (
    extract_claims,
    qa_audit,
    search_exa_for_claim,
    validate_claims,
)
from app.worker.gemini_raw_log import key_alias, log_raw
from app.worker.language import language_delivery_outcome, resolve_language_context
from app.worker.output_variants import (
    CanonicalContentResult,
    OutputVariantType,
    build_canonical_content_result,
    generate_all_variants,
    resolve_telegram_delivery_payload,
)
from app.worker.personal_context import load_personal_context
from app.worker.prioritization import score_content
from app.worker.priority_policy import (
    apply_priority_policy,
    evaluate_priority,
    fallback_priority,
    format_priority_summary,
    policy_observability_payload,
    priority_delivery_outcome,
)
from app.worker.progress import set_progress
from app.worker.schemas import QAResult, VideoAnalysis
from app.worker.specialized_analysis import generate_specialized_analysis
from app.worker.structured_analysis import generate_structured_analysis
from app.worker.visual_analysis import extract_visual_evidence

_original_execute = googleapiclient.http.HttpRequest.execute

def _patched_execute(self, *args, **kwargs):
    if "$discovery" in self.uri and "key=AQ" in self.uri:
        self.uri = self.uri.split("&key=")[0]
    return _original_execute(self, *args, **kwargs)

googleapiclient.http.HttpRequest.execute = _patched_execute

logger = logging.getLogger(__name__)
genai.configure(api_key=settings.gemini_api_key)


def determine_completion_status(delivery_status: dict[str, str]) -> str:
    """DONE means every applicable delivery step succeeded or was idempotently skipped."""
    return (
        "PARTIAL"
        if any(value.startswith("FAILED") for value in delivery_status.values())
        else "DONE"
    )


def deferred_audit_fields() -> dict[str, str | None]:
    """Option B: Post-Publish Audit is not an active production capability."""
    return {"audit_scheduled_at": None, "audit_state": "DEFERRED"}

# Hard per-call timeouts (seconds) so a hung Gemini key rotates instead of
# blocking the whole ARQ job until job_timeout (600s) silently kills it.
CALL_GEN_TIMEOUT = float(os.getenv("GEMINI_GEN_TIMEOUT", "360"))       # generate_content
CALL_PROCESS_TIMEOUT = float(os.getenv("GEMINI_PROCESS_TIMEOUT", "300"))  # upload PROCESSING wait
CALL_UPLOAD_TIMEOUT = float(os.getenv("GEMINI_UPLOAD_TIMEOUT", "120"))  # video upload (File API) itself

async def update_job_status(job_id: str, status: str, **kwargs):
    async with AsyncSessionLocal() as session:
        job = await session.get(Job, job_id)
        if job:
            job.status = status
            # The reaper needs the moment processing actually began — created_at
            # is when the row was inserted, which can be hours earlier when the
            # job sat in QUEUED behind others.
            if status == 'PROCESSING' and job.started_at is None:
                job.started_at = datetime.utcnow()
            for k, v in kwargs.items():
                setattr(job, k, v)
            await session.commit()


def _to_naive_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


async def persist_content_package_models(pkg: ContentPackage) -> None:
    """Persist or update ContentPackage, DeliveryRecords, and PublicationIntents in PostgreSQL."""
    try:
        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        async with AsyncSessionLocal() as session:
            existing = await session.get(ContentPackageModel, pkg.package_id)
            if existing:
                existing.status = pkg.approval_state.value
                existing.distribution_targets = {
                    k: v.model_dump(mode="json") for k, v in pkg.distribution_targets.items()
                }
                existing.updated_at = now_naive
            else:
                model = ContentPackageModel(
                    id=pkg.package_id,
                    job_id=pkg.job_id,
                    source_url=pkg.source_url,
                    contract_version=pkg.contract_version,
                    status=pkg.approval_state.value,
                    language_context=pkg.language_context,
                    router_result=pkg.router_result,
                    priority_result=pkg.priority_result,
                    canonical_content=pkg.canonical_content.model_dump(mode="json"),
                    output_variants=pkg.output_variants.model_dump(mode="json"),
                    distribution_targets={
                        k: v.model_dump(mode="json") for k, v in pkg.distribution_targets.items()
                    },
                    created_at=_to_naive_utc(pkg.created_at) or now_naive,
                    updated_at=now_naive,
                )
                session.add(model)

            for record in pkg.delivery_records:
                rec_existing = await session.get(ContentDeliveryModel, record.delivery_id)
                if not rec_existing:
                    rec_model = ContentDeliveryModel(
                        id=record.delivery_id,
                        package_id=record.package_id,
                        target=record.target.value,
                        variant=record.variant.value,
                        attempt_id=record.attempt_id,
                        status=record.status.value,
                        external_id=record.external_id,
                        idempotency_key=record.idempotency_key,
                        error_code=record.error_code,
                        error_message=record.error_message,
                        started_at=_to_naive_utc(record.started_at) or now_naive,
                        finished_at=_to_naive_utc(record.finished_at),
                        publication_key=record.publication_key,
                        payload_hash=record.payload_hash,
                        provider_post_id=record.provider_post_id,
                        provider_url=record.provider_url,
                        retry_count=record.retry_count,
                        next_retry_at=_to_naive_utc(record.next_retry_at),
                    )
                    session.add(rec_model)
                else:
                    rec_existing.status = record.status.value
                    rec_existing.external_id = record.external_id
                    rec_existing.error_code = record.error_code
                    rec_existing.error_message = record.error_message
                    rec_existing.finished_at = _to_naive_utc(record.finished_at)
                    rec_existing.publication_key = record.publication_key
                    rec_existing.payload_hash = record.payload_hash
                    rec_existing.provider_post_id = record.provider_post_id
                    rec_existing.provider_url = record.provider_url
                    rec_existing.retry_count = record.retry_count
                    rec_existing.next_retry_at = _to_naive_utc(record.next_retry_at)

            for intent in pkg.publication_intents:
                intent_existing = await session.get(PublicationIntentModel, intent.intent_id)
                if not intent_existing:
                    intent_model = PublicationIntentModel(
                        id=intent.intent_id,
                        package_id=intent.package_id,
                        job_id=intent.job_id,
                        target=intent.target.value,
                        variant=intent.variant.value,
                        approved_by=intent.approved_by,
                        approved_at=_to_naive_utc(intent.approved_at) or now_naive,
                        payload_hash=intent.payload_hash,
                        publication_key=intent.publication_key,
                        status=intent.status.value,
                        attempt_count=intent.attempt_count,
                        next_retry_at=_to_naive_utc(intent.next_retry_at),
                        last_error_code=intent.last_error_code,
                        last_error_message=intent.last_error_message,
                        provider_post_id=intent.provider_post_id,
                        provider_url=intent.provider_url,
                        created_at=now_naive,
                        updated_at=now_naive,
                    )
                    session.add(intent_model)
                else:
                    intent_existing.status = intent.status.value
                    intent_existing.attempt_count = intent.attempt_count
                    intent_existing.next_retry_at = _to_naive_utc(intent.next_retry_at)
                    intent_existing.last_error_code = intent.last_error_code
                    intent_existing.last_error_message = intent.last_error_message
                    intent_existing.provider_post_id = intent.provider_post_id
                    intent_existing.provider_url = intent.provider_url
            from app.worker.audit_scheduler import register_audit_target_if_eligible
            for record in pkg.delivery_records:
                target_obj = pkg.distribution_targets.get(record.target.value)
                approved_text = (target_obj.rendered_payload if target_obj else "") or ""
                try:
                    await register_audit_target_if_eligible(
                        session=session,
                        delivery_record=record,
                        approved_text=approved_text,
                        package_id=pkg.package_id,
                    )
                except Exception as audit_reg_err:
                    logger.warning("Could not register audit target for %s: %s", record.delivery_id, audit_reg_err)

            await session.commit()
    except Exception as e:
        logger.error(f"Failed to persist ContentPackage {pkg.package_id}: {e}")


async def execute_publication_intents(package_id: str) -> bool:
    """
    Worker task to execute pending publication intents for a ContentPackage.

    Security & Invariants:
    1. Verifies payload hash: current_hash == approved_hash.
       If mismatched: sets intent status to APPROVAL_STALE and blocks publication.
    2. Enforces connector capability status and account identity.
    3. Handles timeouts safely: sets status to DELIVERY_UNKNOWN and triggers reconciliation.
    4. Records DeliveryRecord with stable publication_key (retry != new publication).
    5. Reconciles package status using reconcile_package_status.
    """
    import datetime as dt_module

    from app.worker.connectors import ConnectorRegistry, sanitize_sensitive_text

    logger.info("Executing publication intents for package %s", package_id)
    now_naive = datetime.now(timezone.utc).replace(tzinfo=None)

    async with AsyncSessionLocal() as session:
        pkg_row = await session.get(ContentPackageModel, package_id)
        if not pkg_row:
            logger.error("Package %s not found for publication", package_id)
            return False

        pkg_obj = ContentPackage.model_validate({
            "package_id": pkg_row.id,
            "job_id": pkg_row.job_id,
            "source_url": pkg_row.source_url,
            "contract_version": pkg_row.contract_version,
            "created_at": pkg_row.created_at,
            "status": pkg_row.status,
            "language_context": pkg_row.language_context or {},
            "router_result": pkg_row.router_result or {},
            "priority_result": pkg_row.priority_result or {},
            "canonical_content": pkg_row.canonical_content,
            "output_variants": pkg_row.output_variants,
            "distribution_targets": pkg_row.distribution_targets,
            "approval_state": pkg_row.status,
            "delivery_records": [],
            "publication_intents": [],
        })

        intents_stmt = select(PublicationIntentModel).where(PublicationIntentModel.package_id == package_id)
        intent_rows = (await session.execute(intents_stmt)).scalars().all()
        for i_row in intent_rows:
            pkg_obj.publication_intents.append(PublicationIntent(
                intent_id=i_row.id,
                package_id=i_row.package_id,
                job_id=i_row.job_id,
                target=TargetPlatform(i_row.target),
                variant=OutputVariantType(i_row.variant),
                approved_by=i_row.approved_by,
                approved_at=i_row.approved_at,
                payload_hash=i_row.payload_hash,
                publication_key=i_row.publication_key,
                status=PublicationIntentStatus(i_row.status),
                attempt_count=i_row.attempt_count or 0,
                next_retry_at=i_row.next_retry_at,
                last_error_code=i_row.last_error_code,
                last_error_message=i_row.last_error_message,
                provider_post_id=i_row.provider_post_id,
                provider_url=i_row.provider_url,
            ))

        deliv_stmt = select(ContentDeliveryModel).where(ContentDeliveryModel.package_id == package_id)
        deliv_rows = (await session.execute(deliv_stmt)).scalars().all()
        for d_row in deliv_rows:
            pkg_obj.delivery_records.append(DeliveryRecord(
                delivery_id=d_row.id,
                package_id=d_row.package_id,
                target=TargetPlatform(d_row.target),
                variant=OutputVariantType(d_row.variant),
                attempt_id=d_row.attempt_id,
                approval_state=PackageStatus(pkg_row.status),
                status=DeliveryOutcome(d_row.status),
                external_id=d_row.external_id,
                started_at=d_row.started_at,
                finished_at=d_row.finished_at,
                error_code=d_row.error_code,
                error_message=d_row.error_message,
                idempotency_key=d_row.idempotency_key,
                publication_key=getattr(d_row, "publication_key", None),
                payload_hash=getattr(d_row, "payload_hash", None),
                provider_post_id=getattr(d_row, "provider_post_id", None),
                provider_url=getattr(d_row, "provider_url", None),
                retry_count=getattr(d_row, "retry_count", 0) or 0,
                next_retry_at=getattr(d_row, "next_retry_at", None),
            ))

    any_attempted = False
    for intent in pkg_obj.publication_intents:
        if intent.status not in (PublicationIntentStatus.PENDING, PublicationIntentStatus.DELIVERY_UNKNOWN):
            continue

        target_obj = pkg_obj.distribution_targets.get(intent.target.value)
        if not target_obj:
            continue

        variants_dict = getattr(pkg_obj.output_variants, "variants", {}) if hasattr(pkg_obj, "output_variants") else {}
        current_v = variants_dict.get(intent.variant.value)
        current_text = (current_v.text if current_v else target_obj.rendered_payload) or ""
        current_hash = compute_payload_hash(current_text)

        if current_hash != intent.payload_hash:
            logger.warning(
                "STALE APPROVAL: Variant %s in package %s has hash %s != approved %s",
                intent.variant.value, package_id, current_hash[:16], intent.payload_hash[:16],
            )
            intent.status = PublicationIntentStatus.APPROVAL_STALE
            intent.last_error_code = "APPROVAL_STALE"
            intent.last_error_message = "Variant text modified after owner approval. Re-approval required."
            target_obj.status = TargetDeliveryStatus.FAILED
            target_obj.error_code = "APPROVAL_STALE"
            target_obj.error_message = intent.last_error_message
            any_attempted = True
            continue

        try:
            connector = ConnectorRegistry.get_connector(intent.target)
        except Exception as e:
            logger.error("No connector for target %s: %s", intent.target, e)
            continue

        caps = connector.capabilities()
        if caps.status.value != "CONNECTED_SUPPORTED":
            if caps.status.value == "UNSUPPORTED_OFFICIAL_API":
                intent.status = PublicationIntentStatus.MANUAL_EXPORT_READY
                target_obj.status = TargetDeliveryStatus.READY_FOR_MANUAL_PUBLISH
            else:
                intent.status = PublicationIntentStatus.SUPPORTED_NOT_CONFIGURED
                target_obj.status = TargetDeliveryStatus.SUPPORTED_NOT_CONFIGURED
            any_attempted = True
            continue

        intent.attempt_count += 1
        attempt_id = sum(1 for r in pkg_obj.delivery_records if r.target == intent.target) + 1
        intent.status = PublicationIntentStatus.IN_FLIGHT

        pub_result = await connector.publish(current_text, intent)
        any_attempted = True

        if pub_result.success:
            intent.status = PublicationIntentStatus.SUCCEEDED
            intent.provider_post_id = pub_result.provider_post_id
            intent.provider_url = pub_result.provider_url
            intent.last_error_code = None
            intent.last_error_message = None

            target_obj.status = TargetDeliveryStatus.DELIVERED
            target_obj.external_id = pub_result.provider_post_id
            target_obj.delivered_at = now_naive

            rec = DeliveryRecord.create(
                package_id=package_id,
                target=intent.target,
                variant=intent.variant,
                attempt_id=attempt_id,
                approval_state=PackageStatus.APPROVED,
                status=DeliveryOutcome.SUCCEEDED,
                external_id=pub_result.provider_post_id,
                finished_at=now_naive,
                publication_key=intent.publication_key,
                payload_hash=intent.payload_hash,
                provider_post_id=pub_result.provider_post_id,
                provider_url=pub_result.provider_url,
                retry_count=intent.attempt_count - 1,
            )
            pkg_obj.delivery_records.append(rec)

        elif pub_result.is_ambiguous:
            intent.status = PublicationIntentStatus.DELIVERY_UNKNOWN
            intent.last_error_code = pub_result.error_code or "TIMEOUT"
            intent.last_error_message = pub_result.error_message
            target_obj.status = TargetDeliveryStatus.DELIVERY_UNKNOWN

            recon = await connector.reconcile_ambiguous_delivery(intent, current_text)
            if recon.resolved and recon.published:
                intent.status = PublicationIntentStatus.SUCCEEDED
                intent.provider_post_id = recon.provider_post_id
                intent.provider_url = recon.provider_url
                target_obj.status = TargetDeliveryStatus.DELIVERED
                target_obj.external_id = recon.provider_post_id
                target_obj.delivered_at = now_naive

                rec = DeliveryRecord.create(
                    package_id=package_id,
                    target=intent.target,
                    variant=intent.variant,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=DeliveryOutcome.SUCCEEDED,
                    external_id=recon.provider_post_id,
                    finished_at=now_naive,
                    publication_key=intent.publication_key,
                    payload_hash=intent.payload_hash,
                    provider_post_id=recon.provider_post_id,
                    provider_url=recon.provider_url,
                    retry_count=intent.attempt_count - 1,
                )
                pkg_obj.delivery_records.append(rec)
            else:
                rec = DeliveryRecord.create(
                    package_id=package_id,
                    target=intent.target,
                    variant=intent.variant,
                    attempt_id=attempt_id,
                    approval_state=PackageStatus.APPROVED,
                    status=DeliveryOutcome.RETRYABLE_ERROR if pub_result.is_retryable else DeliveryOutcome.FAILED,
                    error_code=pub_result.error_code,
                    error_message=sanitize_sensitive_text(pub_result.error_message or ""),
                    finished_at=now_naive,
                    publication_key=intent.publication_key,
                    payload_hash=intent.payload_hash,
                    retry_count=intent.attempt_count - 1,
                )
                pkg_obj.delivery_records.append(rec)

        else:
            intent.last_error_code = pub_result.error_code
            intent.last_error_message = sanitize_sensitive_text(pub_result.error_message or "")

            if pub_result.is_retryable and intent.attempt_count < settings.publish_max_retries:
                intent.status = PublicationIntentStatus.PENDING
                if pub_result.retry_after_seconds and pub_result.retry_after_seconds > 0:
                    delay_sec = pub_result.retry_after_seconds
                else:
                    backoff_idx = min(intent.attempt_count - 1, len(settings.publish_retry_backoff_seconds) - 1)
                    delay_sec = settings.publish_retry_backoff_seconds[backoff_idx]
                intent.next_retry_at = now_naive + dt_module.timedelta(seconds=delay_sec)
                outcome = DeliveryOutcome.RETRYABLE_ERROR
            else:
                intent.status = PublicationIntentStatus.FAILED
                target_obj.status = TargetDeliveryStatus.FAILED
                target_obj.error_code = pub_result.error_code
                target_obj.error_message = intent.last_error_message
                outcome = DeliveryOutcome.FAILED

            rec = DeliveryRecord.create(
                package_id=package_id,
                target=intent.target,
                variant=intent.variant,
                attempt_id=attempt_id,
                approval_state=PackageStatus.APPROVED,
                status=outcome,
                error_code=pub_result.error_code,
                error_message=intent.last_error_message,
                finished_at=now_naive,
                publication_key=intent.publication_key,
                payload_hash=intent.payload_hash,
                retry_count=intent.attempt_count - 1,
                next_retry_at=intent.next_retry_at,
            )
            pkg_obj.delivery_records.append(rec)

    new_state = reconcile_package_status(pkg_obj)
    if new_state != pkg_obj.approval_state and new_state in VALID_TRANSITIONS.get(pkg_obj.approval_state, set()):
        transition_package_status(pkg_obj, new_state, "Publication intents execution completed")

    await persist_content_package_models(pkg_obj)
    return any_attempted



import json as _json

import httpx

COBALT_INSTANCES = [
    "https://co.eepy.today",
    "https://dwnld.nichind.dev",
]

async def _try_cobalt(client: httpx.AsyncClient, base: str, url: str) -> str | None:
    """Try one cobalt instance. Returns download URL or None."""
    try:
        resp = await client.post(
            f"{base}/",
            json={"url": url},
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ReelsBot/1.0",
            },
        )
        if resp.status_code != 200:
            logger.warning(f"Cobalt {base} returned {resp.status_code}")
            return None
        data = resp.json()
        status = data.get("status")
        if status in ("tunnel", "redirect"):
            dl_url = data.get("url")
            if dl_url:
                logger.info(f"Cobalt {base} → {status}: {dl_url[:80]}...")
                return dl_url
        logger.warning(f"Cobalt {base} unexpected status: {status} ({data.get('error', {})})")
        return None
    except Exception as e:
        logger.warning(f"Cobalt {base} error: {e}")
        return None

async def download_video(url: str, output_path: str) -> str:
    # Try cobalt instances first
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        for base in COBALT_INSTANCES:
            dl_url = await _try_cobalt(client, base, url)
            if dl_url:
                logger.info(f"Downloading from cobalt to {output_path}...")
                try:
                    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as dl_client:
                        async with dl_client.stream("GET", dl_url) as stream:
                            stream.raise_for_status()
                            with open(output_path, "wb") as f:
                                async for chunk in stream.aiter_bytes(chunk_size=65536):
                                    f.write(chunk)
                    
                    # Verify size - if < 1MB, it's likely a broken DASH fragment or thumbnail
                    fsize = os.path.getsize(output_path)
                    if fsize < 1_000_000:
                        logger.warning(f"Cobalt {base} returned suspiciously small file ({fsize} bytes). Rejecting.")
                        os.unlink(output_path)
                        continue # try next cobalt or fallback
                        
                    logger.info(f"Downloaded via Cobalt ({base}).")
                    return output_path
                except Exception as e:
                    logger.warning(f"Cobalt download failed from {base}: {e}, trying next...")

    # Fallback to yt-dlp
    logger.info(f"All cobalt instances failed or returned junk. Falling back to yt-dlp for {url}...")
    # Add fake user agent and cookies if needed, but basic usually works
    cmd = ["yt-dlp", "--max-filesize", "50M", "-o", output_path, url]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise Exception(f"Download failed (yt-dlp): {stderr.decode()}")
    logger.info("Downloaded via yt-dlp.")
    return output_path


async def get_video_dimensions(file_path: str) -> tuple[int, int]:
    """Get video width/height via ffprobe. Returns (0, 0) on failure."""
    try:
        cmd = [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams", "-select_streams", "v:0",
            file_path,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        info = _json.loads(stdout.decode())
        stream = info["streams"][0]
        return int(stream["width"]), int(stream["height"])
    except Exception as e:
        logger.warning(f"ffprobe failed: {e}, sending without dimensions")
        return 0, 0


async def downscale_video(input_path: str) -> str:
    """Downscale video to 720p if larger. Returns path (may be same or new)."""
    try:
        w, h = await get_video_dimensions(input_path)
        fsize = os.path.getsize(input_path)
        # only skip if truly small SD
        if w <= 720 and h <= 1280 and fsize < 15_000_000:
            # still force re-encode to progressive mp4 for Gemini compatibility
            if fsize < 2_000_000:
                logger.info(f"Video {w}x{h} {fsize}b small, will re-encode to progressive")
            else:
                logger.info(f"Video {w}x{h} already <=720p, no downscale needed")
                return input_path
        output_path = input_path.replace(".mp4", "_720.mp4")
        # Force progressive H264/AAC for Gemini File API
        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-vf", "scale=720:-2",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "faststart",
            output_path
        ]
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.warning(f"ffmpeg downscale failed: {stderr.decode()[:800]}, trying alternative")
            # fallback: try without scale, just remux to progressive
            cmd2 = [
                "ffmpeg", "-y", "-i", input_path,
                "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                "-c:a", "aac",
                "-movflags", "faststart",
                output_path
            ]
            proc2 = await asyncio.create_subprocess_exec(*cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, stderr2 = await proc2.communicate()
            if proc2.returncode != 0:
                logger.warning(f"ffmpeg fallback also failed: {stderr2.decode()[:500]}, using original")
                return input_path
        new_w, new_h = await get_video_dimensions(output_path)
        new_sz = os.path.getsize(output_path) if os.path.exists(output_path) else 0
        logger.info(f"Downscaled {w}x{h} {fsize}b -> {new_w}x{new_h} {new_sz}b: {output_path}")
        return output_path
    except Exception as e:
        logger.warning(f"downscale_video error: {e}, using original")
        return input_path



def _gemini_key_pool() -> tuple[list[str], str | None, str | None]:
    """Shared rotation pool: free-tier keys first, paid key last fallback."""
    main_key = getattr(settings, "gemini_api_key", None)
    if main_key:
        main_key = main_key.strip()
    paid_key = os.getenv("GEMINI_PAID_KEY")
    if paid_key:
        paid_key = paid_key.strip()
    # pool: FREE-TIER FIRST (main + key_1..N), paid LAST as fallback when all
    # free-tier keys are rate-limited/exhausted (cheaper at current volume).
    pool = []
    if main_key:
        pool.append(main_key)
    for i in range(1, 10):
        k = os.getenv(f"GEMINI_API_KEY_{i}")
        if k:
            k = k.strip()
            if k not in pool:
                pool.append(k)
    if paid_key and paid_key not in pool:
        pool.append(paid_key)
    return pool, main_key, paid_key


async def transcribe_with_legacy_gemini(file_path: str, max_rounds: int = 8, base_delay: float = 3.0, cap_delay: float = 120.0) -> str:
    """Extract raw transcript with multi-round Gemini key rotation.

    Rotation pool includes the MAIN GEMINI_API_KEY even when GEMINI_API_KEY_1..N
    are set (the main key used to be shadowed). Every generateContent attempt is
    logged RAW (status + body + timestamps + key alias) to gemini_rotation_debug.log.
    """
    pool, main_key, paid_key = _gemini_key_pool()
    if not pool:
        raise RuntimeError("No GEMINI_API_KEY set")

    model = settings.gemini_model
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    extraction_prompt = "Сделай полную подробную транскрипцию всего, что говорят в этом видео. Верни только текст транскрипта аудио, без описания визуального контента."

    last_error = None
    for round_idx in range(1, max_rounds + 1):
        for key in pool:
            alias = key_alias(key, main_key, paid_key)
            genai.configure(api_key=key, transport="rest")
            logger.info(f"Trying Gemini API key ending in ...{key[-4:] if key else 'None'} ({alias}, Round {round_idx}/{max_rounds})")

            video_file = None
            req_ts = time.time()
            try:
                def _upload_and_analyze():
                    return genai.upload_file(path=file_path)

                # Hard timeout on the upload itself: a hung/slow upload (e.g. flaky
                # network) must rotate to the next key instead of pinning the whole
                # job until job_timeout. TimeoutError is caught by the handler below.
                video_file = await asyncio.wait_for(
                    asyncio.to_thread(_upload_and_analyze), timeout=CALL_UPLOAD_TIMEOUT
                )

                processing_deadline = time.monotonic() + CALL_PROCESS_TIMEOUT
                while video_file.state.name == "PROCESSING":
                    if time.monotonic() > processing_deadline:
                        raise TimeoutError(f"Gemini video processing timeout after {CALL_PROCESS_TIMEOUT}s")
                    await asyncio.sleep(3)
                    video_file = await asyncio.to_thread(genai.get_file, video_file.name)

                if video_file.state.name == "FAILED":
                    err_detail = getattr(video_file, 'error', None)
                    raise Exception(f"Gemini video processing failed: {err_detail}")

                payload = {
                    "contents": [
                        {
                            "role": "user",
                            "parts": [
                                {"file_data": {"file_uri": video_file.uri or video_file.name, "mime_type": video_file.mime_type or "video/mp4"}},
                                {"text": extraction_prompt},
                            ],
                        }
                    ]
                }
                req_ts = time.time()
                async with httpx.AsyncClient(timeout=CALL_GEN_TIMEOUT) as client:
                    resp = await client.post(
                        url,
                        headers={"x-goog-api-key": key, "Accept": "application/json"},
                        json=payload,
                    )
                end_ts = time.time()
                log_raw(alias, model, url, resp.status_code, resp.text, req_ts, end_ts)

                if resp.status_code == 200:
                    data = resp.json()
                    cands = data.get("candidates", [])
                    if not cands or not cands[0].get("content", {}).get("parts"):
                        # Possibly blocked / no content — raise to be surfaced
                        raise RuntimeError(f"Gemini returned empty content for {alias}: {resp.text[:400]}")
                    text = "".join(p.get("text", "") for p in cands[0]["content"]["parts"])
                    if text:
                        return text
                    raise RuntimeError(f"Gemini returned empty text for {alias}")

                # transient -> rotate
                if resp.status_code in (429, 500, 502, 503, 504):
                    logger.warning(f"Gemini key ({alias}) returned {resp.status_code}. Rotating...")
                    last_error = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                    continue
                # hard config/validation error -> give up on this key, raise to job
                logger.error(f"Gemini key ({alias}) HTTP {resp.status_code}: {resp.text[:300]}")
                raise RuntimeError(f"Gemini HTTP {resp.status_code} on {alias}: {resp.text[:300]}")

            except (httpx.TimeoutException, asyncio.TimeoutError, TimeoutError) as e:
                end_ts = time.time()
                log_raw(alias, model, url, -1, f"{type(e).__name__}: {e}", req_ts, end_ts)
                logger.warning(f"Gemini key ({alias}) timed out: {e}. Rotating...")
                last_error = e
                continue
            except Exception as e:
                error_msg = str(e).lower()
                end_ts = time.time()
                log_raw(alias, model, url, 0, f"{type(e).__name__}: {e}", req_ts, end_ts)
                is_timeout = "timeout" in error_msg or "timed out" in error_msg
                if is_timeout or any(err_kw in error_msg for err_kw in ("429", "quota", "exhausted", "503", "high demand", "unavailable", "rate limit", "read operation")):
                    logger.warning(f"Gemini key ({alias}) rate limited / busy: {e}. Rotating...")
                    last_error = e
                    continue
                else:
                    raise e
            finally:
                if video_file is not None:
                    try:
                        await asyncio.to_thread(genai.delete_file, video_file.name)
                    except Exception as del_err:
                        logger.debug(f"Failed to delete uploaded video file {getattr(video_file, 'name', 'unknown')}: {del_err}")

        # If all keys were rate-limited in this round
        if round_idx < max_rounds:
            temp = min(cap_delay, base_delay * (2 ** (round_idx - 1)))
            wait_time = random.uniform(temp * 0.5, temp)
            logger.warning(f"All Gemini API keys rate limited/busy in get_raw_transcript. Sleeping {wait_time:.2f}s before retry round {round_idx + 1}/{max_rounds}...")
            await asyncio.sleep(wait_time)

    raise last_error or Exception("All API keys failed in get_raw_transcript")


async def get_transcript_with_meta(
    file_path: str, max_rounds: int = 8, base_delay: float = 3.0, cap_delay: float = 120.0
) -> tuple[str, dict]:
    """Hybrid orchestrator returning (text, meta).

    Implements §8-§9 conservative selection and §16 logging.
    Meta keys: model, fallback_used, status, duration, latency, chars.
    """
    from app.worker.transcribe_35 import (
        assess_transcript_completeness,
        transcribe_with_gemini_35,
    )
    from app.worker.visual_analysis import get_video_duration

    if not getattr(settings, "transcription_primary_enabled", True):
        t0 = time.monotonic()
        text = await transcribe_with_legacy_gemini(file_path, max_rounds, base_delay, cap_delay)
        try:
            duration = await get_video_duration(file_path)
        except Exception:
            duration = 0.0
        meta = {
            "model": getattr(settings, "transcription_fallback_model", "legacy"),
            "fallback_used": "flag_off",
            "status": "OK",
            "duration": duration,
            "latency": time.monotonic() - t0,
            "chars": len(text or ""),
        }
        logger.info(f"Transcription flag OFF -> legacy ({meta['chars']} chars)")
        return text, meta

    try:
        duration = await get_video_duration(file_path)
    except Exception:
        duration = 0.0

    t0 = time.monotonic()
    try:
        pool, _, _ = _gemini_key_pool()
        text = await transcribe_with_gemini_35(
            file_path, pool, tmp_dir=os.path.dirname(file_path) or "/tmp"
        )
        status = assess_transcript_completeness(text, duration)
        latency = time.monotonic() - t0
        if status == "FAILED":
            logger.warning(f"3.5 Transcribe FAILED ({len(text)} chars, {duration:.0f}s): fallback")
            fb_text = await transcribe_with_legacy_gemini(file_path, max_rounds, base_delay, cap_delay)
            meta = {"model": settings.transcription_primary_model, "fallback_used": "primary_failed",
                    "status": "FAILED", "duration": duration, "latency": latency, "chars": len(fb_text)}
            logger.info(f"Transcription fallback used ({len(fb_text)} chars, reason=FAILED)")
            return fb_text, meta
        if status == "SUSPICIOUS":
            logger.warning(f"3.5 SUSPICIOUS ({len(text)} chars, {duration:.0f}s): trying fallback for comparison")
            try:
                fb_text = await transcribe_with_legacy_gemini(file_path, max_rounds, base_delay, cap_delay)
                # conservative: keep primary unless fallback is dramatically longer (>1.8x) and fallback is OK
                fb_status = assess_transcript_completeness(fb_text, duration)
                if fb_status == "OK" and len(fb_text) > len(text) * 1.8:
                    meta = {"model": settings.transcription_primary_model, "fallback_used": "suspicious_overridden",
                            "status": "SUSPICIOUS", "duration": duration, "latency": latency, "chars": len(fb_text)}
                    logger.warning(f"SUSPICIOUS overridden by fallback ({len(text)} -> {len(fb_text)})")
                    return fb_text, meta
                meta = {"model": settings.transcription_primary_model, "fallback_used": "none",
                        "status": "SUSPICIOUS", "duration": duration, "latency": latency, "chars": len(text)}
                logger.info(f"3.5 SUSPICIOUS kept primary ({len(text)} chars) with warning")
                return text, meta
            except Exception as fb_e:
                logger.warning(f"SUSPICIOUS fallback also failed: {fb_e}, keeping primary")
                meta = {"model": settings.transcription_primary_model, "fallback_used": "none",
                        "status": "SUSPICIOUS", "duration": duration, "latency": latency, "chars": len(text)}
                return text, meta
        meta = {"model": settings.transcription_primary_model, "fallback_used": "none",
                "status": "OK", "duration": duration, "latency": latency, "chars": len(text)}
        logger.info(f"3.5 Transcribe OK ({len(text)} chars, {latency:.1f}s, {duration:.0f}s audio)")
        return text, meta
    except Exception as e:
        logger.warning(f"3.5 Transcribe primary failed, falling back: {e}")
    fb_text = await transcribe_with_legacy_gemini(file_path, max_rounds, base_delay, cap_delay)
    meta = {"model": settings.transcription_fallback_model if hasattr(settings, "transcription_fallback_model") else "legacy",
            "fallback_used": "primary_exception", "status": "FALLBACK", "duration": duration,
            "latency": time.monotonic() - t0, "chars": len(fb_text)}
    logger.info(f"Fallback transcription used ({len(fb_text)} chars)")
    return fb_text, meta


async def get_raw_transcript(file_path: str, max_rounds: int = 8, base_delay: float = 3.0, cap_delay: float = 120.0) -> str:
    """Legacy entry point, kept for backward compatibility (tests & callers).

    Delegates to get_transcript_with_meta and returns only the text.
    """
    text, _meta = await get_transcript_with_meta(file_path, max_rounds, base_delay, cap_delay)
    return text


def _format_independent_analysis_layers(analysis: VideoAnalysis) -> str:
    """Render independent Fact Check and Business Check layers separately."""
    parts = ["🔎 **НЕЗАВИСИМАЯ ПРОВЕРКА УТВЕРЖДЕНИЙ (FACT CHECK):**"]
    for c in analysis.claims:
        if c.status == "подтверждено":
            parts.append(f"- ✅ [Подтверждено] {c.statement}\n  (Источник: [{c.source_type}] {c.source_url})")
        elif c.status == "опровергнуто":
            parts.append(f"- ❌ [Опровергнуто] {c.statement}\n  (Источник: {c.source_url})")
        elif c.status == "не проверено":
            parts.append(f"- 🟡 [Не проверено] {c.statement} ({c.unverified_reason or 'Нет надежных источников'})")

    if getattr(analysis, 'business_check', None):
        parts.append(format_business_check_markdown(analysis.business_check))

    return "\n\n".join(parts)


def _format_fact_check_only(analysis: VideoAnalysis) -> str:
    parts = ["🔎 **НЕЗАВИСИМАЯ ПРОВЕРКА УТВЕРЖДЕНИЙ (FACT CHECK):**"]
    if not analysis.claims:
        parts.append("Проверяемые утверждения не выделены.")
    for claim in analysis.claims:
        if claim.status == "подтверждено":
            parts.append(f"- ✅ [Подтверждено] {claim.statement}\n  (Источник: [{claim.source_type}] {claim.source_url})")
        elif claim.status == "опровергнуто":
            parts.append(f"- ❌ [Опровергнуто] {claim.statement}\n  (Источник: {claim.source_url})")
        elif claim.status == "не проверено":
            parts.append(f"- 🟡 [Не проверено] {claim.statement} ({claim.unverified_reason or 'Нет надежных источников'})")
    return "\n\n".join(parts)


def _compose_analysis_output(structured_analysis: str | None, analysis: VideoAnalysis, raw_video_text: str) -> str:
    """Compose canonical structured output or an explicit legacy fallback."""
    source_material_text = "### 📝 ДОСТУПНЫЙ МАТЕРИАЛ ВИДЕО\n" + raw_video_text[:1000] + "..."
    if structured_analysis:
        return structured_analysis + "\n\n" + _format_independent_analysis_layers(analysis)

    fallback_material = (
        "⚠️ **STRUCTURED ANALYSIS UNAVAILABLE**\n\n"
        + source_material_text
        + "\nRaw transcript is source material, not reconstructed mechanics."
    )
    return format_analysis_markdown(analysis, fallback_material)


def format_analysis_markdown(analysis: VideoAnalysis, mechanics_text: str) -> str:
    parts = []
    if mechanics_text:
        parts.append(mechanics_text)
    parts.append(_format_independent_analysis_layers(analysis))

    if analysis.task_description:
        parts.append(f"\nЗАДАЧА:\n{analysis.task_description}")

    return "\n\n".join(parts)

def format_error_text(e: BaseException, limit: int = 1500) -> str:
    """Never let a job land in ERROR with an empty or useless reason.

    Some exceptions stringify to "" (bare `raise Exception()`, several httpx /
    aiohttp edge cases). That is how rows ended up with a status and no cause —
    there is literally nothing to debug. Always keep the exception type, and
    attach a traceback when the message itself says nothing.
    """
    msg = str(e).strip()
    head = f"{type(e).__name__}: {msg}" if msg else type(e).__name__
    if not msg:
        tb = traceback.format_exc().strip()
        if tb:
            head = f"{head}\n{tb}"
    return head[:limit]


def _split_text(text: str, limit: int = 4096) -> list[str]:
    """Split long text into chunks under Telegram's message limit."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind('\n\n', 0, limit)
        if split_at < limit // 2:
            split_at = text.rfind('\n', 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(text[:split_at].strip())
        text = text[split_at:].strip()
    if text:
        chunks.append(text)
    return chunks


def _extract_summary(analysis: str) -> str:
    """Extract only the short 1-2 sentence summary for the public channel."""
    start_marker = "КРАТКО ДЛЯ КАНАЛА:"
    start_idx = analysis.find(start_marker)

    if start_idx != -1:
        text_after = analysis[start_idx + len(start_marker):].strip()
        for sep in ['\n---', '\n\n---', '\n1.', '\n**1.', '\n### 1.', '\n\n1.']:
            idx = text_after.find(sep)
            if 0 < idx < len(text_after):
                text_after = text_after[:idx].strip()
                break
        text_after = text_after.strip('-').strip()
        if text_after:
            return text_after

    first = analysis.split('\n\n')[0][:500].strip()
    return first.strip('-').strip()


GENERIC_TITLES = {
    "идея из видео", "идея из видео (reels)", "идея из текста", "idea from video", "reel idea", "новая задача",
    "разбор видео", "видео разбор", "задача из рилз", "идея", "задача", "видео", "анализ видео"
}


def clean_title_str(t: str) -> str:
    if not t:
        return ""
    t = re.sub(r"^[#\*\-\s\d\.\:\(\)\[\]]+", "", t).strip()
    
    prefixes = [
        r"^идея\s+из\s+видео\s*[:\-–—]?",
        r"^идея\s+из\s+текста\s*[:\-–—]?",
        r"^idea\s+brief\s*[:\-–—]?",
        r"^idea\s+from\s+video\s*[:\-–—]?",
        r"^reel\s+idea\s*[:\-–—]?",
        r"^новая\s+задача\s*[:\-–—]?",
        r"^задача\s+для\s+михаила\s*[:\-–—\(\)]*",
        r"^задача\s*[:\-–—]?",
        r"^концепция\s*[:\-–—]?",
        r"^концепт\s*[:\-–—]?",
        r"^дельта\s+механики\s*[:\-–—]?",
        r"^разбор\s+видео\s*[:\-–—]?",
        r"^разбор\s*[:\-–—]?",
        r"^обзор\s*[:\-–—]?",
        r"^суть\s*[:\-–—]?",
        r"^цель\s*[:\-–—]?",
        r"^идея\s*[:\-–—]?",
        r"^как\s+применить\s*[:\-–—\(\)]*",
        r"^как\s+михаил\s+может\s+это\s+применить\s*[:\-–—\(\)]*",
        r"^применение\s+в\s+стеке\s*[:\-–—\(\)]*",
        r"^применение\s+в\s+работе\s+и\s+жизни\s*[:\-–—\(\)]*",
        r"^применение\s+в\s+работе\s*[:\-–—\(\)]*",
        r"^применение\s+и\s+интеграция\s*[:\-–—\(\)]*",
        r"^применение\s+идеи\s*[:\-–—\(\)]*",
        r"^применение\s*[:\-–—\(\)]*",
        r"^интеграция\s+в\s+стек\s*[:\-–—\(\)]*",
        r"^чистый\s+концентрат\s+для\s+инженера\s*[:\-–—\(\)]*",
        r"^белая\s+авто-система\s+ревью\s+фильмов\s*[:\-–—\(\)]*",
        r"^видео\s+демонстрирует\s+(?:три|3|две|2|четыре|4|пять|5)?\s*",
        r"^видео\s+показывает\s+",
        r"^видео\s+разбирает\s+",
        r"^автор\s+демонстрирует\s+",
        r"^автор\s+показывает\s+",
        r"^автор\s+презентует\s+",
        r"^автор\s+делится\s+",
        r"^автор\s+видео\s+развенчивает\s+миф\s+[^,]+,\s*(?:предлагая\s+)?",
        r"^схема\s+описывает\s+",
        r"^в\s+видео\s+демонстрируется\s+",
        r"^в\s+видео\s+показывается\s+",
        r"^в\s+видео\s+представлен\s+разбор\s+",
        r"^разбор\s+репозитория-агрегатора\s*\(?",
        r"^разбор\s+одного\s+из\s+лучших\s+github-репозиториев\s*",
        r"^разбор\s+классической\s+инфобизнесовой\s+воронки\s*[:\-]?\s*",
        r"^разбор\s+классического\s+эстетического\s+лайфстайл-видео\s*\(?[^\)]*\)?\s*,?\s*",
        r"^разбор\s+",
        r"^обзор\s+",
        r"^способ\s+",
        r"^пошаговая\s+методология\s+",
        r"^пошаговый\s+сборка\s+",
        r"^пошаговая\s+сборка\s+",
        r"^михаил,\s+эти\s+три\s+концепта\s+идеально\s+ложатся\s+на\s+твои\s+рабочие\s+процессы\s*[^:]*:\s*",
        r"^михаил,\s+тебе\s+не\s+нужно\s+[^.]*\.\s*(?:твоя\s+ценность\s*—\s*)?",
        r"^михаил,\s+для\s+тебя\s+это\s+[^,]*,\s*а\s*",
        r"^михаил,\s+не\s+трать\s+время\s+[^.]*\.\s*(?:но\s+сам\s+)?",
        r"^михаил\s+может\s+превратить\s+эту\s+ручную\s+рутину\s+из\s+видео\s+в\s*",
        r"^этот\s+ручной\s+процесс\s*—\s*идеальный\s+кандидат\s+на\s*",
        r"^тебе,\s*как\s+[^.]*,\s*",
        r"^тебе\s+не\s+нужен\s+[^.]*\.\s*",
    ]
    
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            new_t = re.sub(p, "", t, flags=re.IGNORECASE).strip()
            if new_t != t:
                t = new_t
                changed = True
                t = re.sub(r"^[\*\-\s\d\.\:\(\)\[\]\"'«»—–]+", "", t).strip()

    t = t.replace("**", "").replace("*", "").replace("`", "").replace("«", "").replace("»", "").replace('"', '').strip()
    t = re.sub(r"^\((?:как\s+)?михаил\s+может\s+это\s+применить\)", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^\((?:применение\s+в\s+работе|применение\s+в\s+стеке|применение|интеграция\s+в\s+стек|применение\s+идеи)\)", "", t, flags=re.IGNORECASE).strip()
    t = re.sub(r"^[\:\-\–—\s]+", "", t).strip()
    t = re.sub(r"https?://\S+", "", t).strip()
    t = t.rstrip(" :.-")
    
    if t:
        t = t[0].upper() + t[1:]
        
    return t


def is_valid_title(title: str) -> bool:
    if not title:
        return False
    t_clean = title.lower().strip()
    if t_clean in GENERIC_TITLES:
        return False
    if len(t_clean) < 8:
        return False
    words = title.split()
    if len(words) < 3:
        return False
    banned_starts = ["михаил,", "михаил ", "тебе, как", "тебе не нужен", "этот ручной", "видео интересно", "поскольку видео"]
    return not any(t_clean.startswith(b) for b in banned_starts)


def truncate_to_words(title: str, min_words: int = 4, max_words: int = 10) -> str:
    words = title.split()
    if len(words) <= max_words:
        return title
    
    for sep in [":", " — ", " – ", " - ", ",", ";", "."]:
        idx = title.find(sep)
        if idx > 15:
            cand = title[:idx].strip()
            if min_words <= len(cand.split()) <= max_words:
                return cand.rstrip(" ,.-")
                
    return " ".join(words[:max_words]).rstrip(" ,.-")


def extract_tasks_from_analysis(analysis: str, url: str = "") -> list[dict]:
    """Parse analysis and return list of actionable tasks with concise, meaningful titles (4-10 words)."""
    # 1. Check if multiple explicit ЗАДАЧА blocks exist
    z_matches = list(re.finditer(r"(?:^|\n)(?:ЗАДАЧА(?:\s+\d+)?|###\s*6(?:\.\d+)?\.?\s*ЗАДАЧА.*?):\s*", analysis, re.IGNORECASE))
    
    if len(z_matches) > 1:
        tasks = []
        for i, match in enumerate(z_matches):
            start = match.end()
            end = z_matches[i + 1].start() if i + 1 < len(z_matches) else len(analysis)
            section_text = analysis[start:end].strip()
            first_line = section_text.splitlines()[0].strip() if section_text.splitlines() else ""
            title = clean_title_str(first_line)
            if not is_valid_title(title):
                title = clean_title_str(section_text[:120])
            title = truncate_to_words(title)
            tasks.append({
                "title": title,
                "description": section_text,
                "source_type": "reel",
                "source_url": url
            })
        return tasks

    # 2. Check Section 6
    m_task = re.search(r"(?:###\s*6\.?\s*ЗАДАЧА[^\n]*\n|ЗАДАЧА:[^\n]*\n)(.*?)(?=(?:\n###\s+[0-9]|\n---\s*\n###|\Z))", analysis, re.DOTALL | re.IGNORECASE)
    if m_task:
        task_block = m_task.group(1).strip()
        # Look for explicit Concept / Idea line
        m_c = re.search(r"(?:^|\n)\s*(?:#{1,6}\s*)?(?:\*\*)?(?:концепция|концепт|идея)(?:\*\*)?\s*[:\*\#\s]*([^\n]+)", task_block, re.IGNORECASE)
        if m_c:
            cand = clean_title_str(m_c.group(1))
            if is_valid_title(cand):
                return [{
                    "title": truncate_to_words(cand),
                    "description": task_block,
                    "source_type": "reel",
                    "source_url": url
                }]

        # Look for numbered sub-tasks that represent distinct skills/tools
        numbered_items = list(re.finditer(r"(?:^|\n)\s*(\d+)\.\s+\*\*([^\*]+)\*\*(.*?)(?=(?:\n\s*\d+\.\s+\*\*|\n####|\n---|\Z))", task_block, re.DOTALL))
        if len(numbered_items) >= 2:
            sub_titles = [clean_title_str(m.group(2)) for m in numbered_items]
            if all(is_valid_title(st) for st in sub_titles):
                tasks = []
                for m in numbered_items:
                    t_title = truncate_to_words(clean_title_str(m.group(2)))
                    t_desc = m.group(0).strip()
                    tasks.append({
                        "title": t_title,
                        "description": t_desc,
                        "source_type": "reel",
                        "source_url": url
                    })
                return tasks
                
        # Check first actionable line
        for line in task_block.splitlines():
            line_str = line.strip()
            if not line_str or line_str.startswith(("---", "#")):
                continue
            cand = clean_title_str(line_str)
            if is_valid_title(cand):
                return [{
                    "title": truncate_to_words(cand),
                    "description": task_block,
                    "source_type": "reel",
                    "source_url": url
                }]

    # 3. Look for Summary (КРАТКО ДЛЯ КАНАЛА:)
    m_sum = re.search(r"(?:КРАТКО|СУТЬ|ОПИСАНИЕ)(?:\s*ДЛЯ\s*КАНАЛА)?\s*\*?\*?:\s*\*?\*?\s*(.*?)(?:\n\n|\n#|\n---)", analysis, re.DOTALL | re.IGNORECASE)
    if m_sum:
        sum_text = m_sum.group(1).strip().replace("\n", " ")
        first_sent = re.split(r"[\.\!\?]\s+", sum_text)[0].strip()
        cand = clean_title_str(first_sent)
        if is_valid_title(cand):
            return [{
                "title": truncate_to_words(cand),
                "description": analysis,
                "source_type": "reel",
                "source_url": url
            }]

    # 4. Look for Section 1 (О ЧЁМ ВИДЕО)
    m_about = re.search(r"###\s*1\.?\s*О\s*ЧЁМ\s*ВИДЕО[^\n]*\n(.*?)(?:\n###|\Z)", analysis, re.DOTALL | re.IGNORECASE)
    if m_about:
        about_text = m_about.group(1).strip()
        m_idea = re.search(r"(?:Идея\s*/\s*Концепция|Концепция|Проблема\s*/\s*Возможность|Содержание)\s*[:\*\#]*\s*([^\n\*\#]+)", about_text, re.IGNORECASE)
        if m_idea:
            cand = clean_title_str(m_idea.group(1))
            if is_valid_title(cand):
                return [{
                    "title": truncate_to_words(cand),
                    "description": analysis,
                    "source_type": "reel",
                    "source_url": url
                }]

    return [{
        "title": "Интеграция решения из видео",
        "description": analysis,
        "source_type": "reel",
        "source_url": url
    }]


def _extract_task(analysis: str) -> dict | None:
    """Backward-compatible helper returning single task or None."""
    tasks = extract_tasks_from_analysis(analysis)
    return tasks[0] if tasks else None


def extract_routed_tasks(task_description: str | None, url: str = "") -> list[dict]:
    """Build tasks only from an explicit routed action, preserving its title."""
    if not task_description:
        return []
    first_line = task_description.splitlines()[0].strip()
    match = re.match(r"(?:\*\*)?ЗАДАЧА:\s*(.+?)(?:\*\*)?$", first_line, re.IGNORECASE)
    if match:
        title = truncate_to_words(clean_title_str(match.group(1)))
        if is_valid_title(title):
            return [{
                "title": title,
                "description": task_description,
                "source_type": "reel",
                "source_url": url,
            }]
    return extract_tasks_from_analysis(task_description, url=url)


async def send_long_text(bot: Bot, chat_id: int, text: str):
    """Send text that may exceed Telegram's 4096 char limit."""
    for chunk in _split_text(text):
        await bot.send_message(chat_id=chat_id, text=chunk)
        await asyncio.sleep(0.3)


async def process_video(ctx, job_id: str, url: str, user_id: int):
    await update_job_status(job_id, 'PROCESSING')
    started_monotonic = time.monotonic()
    tmp_dir = f"/tmp/reels_bot/{job_id}"
    os.makedirs(tmp_dir, exist_ok=True)
    video_path = f"{tmp_dir}/video.mp4"

    try:
        await set_progress(job_id, "DOWNLOAD")
        logger.info(f"Downloading {url} for job {job_id}")
        await download_video(url, video_path)
        
        width, height = await get_video_dimensions(video_path)
        logger.info(f"Video dimensions: {width}x{height}, size: {os.path.getsize(video_path)} bytes")

        # Auto-downscale if >720p to avoid Gemini FAILED on high-res reels
        if width > 720 or height > 1280:
            video_path = await downscale_video(video_path)
            width, height = await get_video_dimensions(video_path)
            logger.info(f"After downscale: {width}x{height}, size: {os.path.getsize(video_path)} bytes")

        logger.info(f"Analyzing {video_path}")
        
        logger.info(f"Extracting raw transcript for {video_path}")
        await set_progress(job_id, "TRANSCRIPT")
        raw_video_text, tmeta = await get_transcript_with_meta(video_path)
        # Canonical artifact: persist immediately, before structured analysis,
        # fact-check and report composition. Downstream stages must never
        # overwrite it; a later ERROR/REVIEW keeps the transcript available.
        await update_job_status(
            job_id, 'PROCESSING',
            full_transcript=raw_video_text,
            transcription_model=tmeta.get("model"),
            transcription_fallback_used=tmeta.get("fallback_used"),
            transcription_status=tmeta.get("status"),
        )

        # Extract visual evidence from video frames for structured analysis
        visual_evidence = await extract_visual_evidence(video_path)

        language_context = await resolve_language_context(
            raw_video_text,
            transcription_meta=tmeta,
            visual_evidence=visual_evidence,
            timeout_seconds=settings.language_detection_timeout_seconds,
        )
        await update_job_status(
            job_id,
            "PROCESSING",
            qa_reasons={"language": language_context.model_dump()},
        )
        logger.info(
            "Language context: detected=%s sources=%s mixed=%s output=%s "
            "translation=%s fallback=%s",
            language_context.detected_language_code,
            language_context.source_languages,
            language_context.is_mixed_language,
            language_context.user_output_language_code,
            language_context.translation_required,
            language_context.fallback_reason,
        )

        # Classify first. All expensive downstream work is controlled by a
        # deterministic policy derived from this decision.
        try:
            await set_progress(job_id, "ANALYSIS")
            route = await route_content(
                raw_video_text, visual_evidence, language_context
            )
        except Exception as router_err:
            logger.error(f"Content Router failed, using compatibility policy: {router_err}")
            route = fallback_route(str(router_err))

        publish_threshold = settings.publish_threshold
        deprioritize_threshold = settings.deprioritize_threshold
        try:
            priority_score = await asyncio.wait_for(
                score_content(raw_video_text, route.summary, language_context),
                timeout=settings.prioritization_timeout_seconds,
            )
            priority = evaluate_priority(
                priority_score,
                publish_threshold=publish_threshold,
                deprioritize_below=deprioritize_threshold,
            )
        except Exception as priority_err:
            logger.error(
                "Prioritization failed; preserving Router safety policy: %s",
                priority_err,
            )
            priority = fallback_priority(
                priority_err,
                publish_threshold=publish_threshold,
                deprioritize_below=deprioritize_threshold,
            )

        combined_policy = apply_priority_policy(route, priority)
        policy = combined_policy.effective_policy
        policy_payload = {
            "language": language_context.model_dump(),
            **policy_observability_payload(route, combined_policy),
        }
        # Persist the decision boundary before any expensive downstream work so
        # an interrupted job still explains what Router and priority decided.
        await update_job_status(job_id, "PROCESSING", qa_reasons=policy_payload)
        logger.info(
            "Combined policy: primary=%s risk=%s priority=%s gate=%s "
            "fact=%s strict=%s business=%s technical=%s tasks=%s channel=%s",
            route.primary_type, route.risk, priority.tier, priority.decision,
            policy.run_fact_check,
            policy.strict_fact_check, policy.run_business_check,
            policy.include_technical_details, policy.create_tasks,
            combined_policy.channel_publication,
        )

        # The former deep structured report remains an on-demand technical
        # detail, and is generated only for routes where it adds value.
        structured_analysis = None
        if policy.include_technical_details:
            try:
                structured_analysis = await generate_structured_analysis(
                    raw_video_text, visual_evidence, language_context
                )
                logger.info("Technical structured analysis generated.")
            except Exception as structured_err:
                logger.error(f"Technical structured analysis failed (non-blocking): {structured_err}")

        analysis_obj = VideoAnalysis(claims=[], viable_idea=False)
        fact_check_text = None
        business_check_text = None
        qa_res = None

        # LOW-risk content does not spend external-search calls. HIGH-risk
        # content fails closed when strict evidence infrastructure is absent.
        if policy.run_fact_check:
            await set_progress(job_id, "VERIFY")
            if not getattr(settings, 'exa_api_key', None):
                reason = "EXA_API_KEY отсутствует: независимая проверка не выполнена."
                fact_check_text = f"🔎 **FACT CHECK:**\n{reason}"
                if policy.strict_fact_check:
                    qa_res = QAResult(approved=False, reasons=[reason])
            else:
                claims = await extract_claims(raw_video_text, language_context)
                fact_claims = [claim for claim in claims if claim.claim_type == "fact"]
                search_data = {}
                for claim in fact_claims:
                    search_data[claim.statement] = await search_exa_for_claim(claim)
                analysis_obj = await validate_claims(claims, search_data)
                fact_check_text = _format_fact_check_only(analysis_obj)
                if policy.strict_fact_check:
                    if not fact_claims:
                        qa_res = QAResult(
                            approved=False,
                            reasons=["HIGH-risk материал не дал проверяемых fact claims."],
                        )
                    else:
                        qa_res = await qa_audit(analysis_obj)

        if policy.run_business_check:
            logger.info("Executing routed Business Check...")
            try:
                bc_res = await run_business_check(
                    transcript=raw_video_text,
                    claims=analysis_obj.claims,
                    factcheck_analysis=analysis_obj,
                    language_context=language_context,
                )
                analysis_obj.business_check = bc_res
                business_check_text = format_business_check_markdown(bc_res)
            except Exception as bc_err:
                logger.error(f"Business Check failed (non-blocking): {bc_err}")

        personal_context = load_personal_context() if policy.run_personal_relevance else {
            "status": "NOT_APPLICABLE", "evidence": []
        }
        try:
            specialized = await generate_specialized_analysis(
                transcript=raw_video_text,
                visual_evidence=visual_evidence,
                route=route,
                policy=policy,
                personal_context=personal_context,
                fact_check_text=fact_check_text or "Не запускался по policy.",
                business_check_text=business_check_text or "Не запускался по policy.",
                language_context=language_context,
            )
            analysis = render_compact_analysis(route, specialized)
            detail_sections = build_detail_sections(
                specialized, fact_check_text, business_check_text, structured_analysis
            )
        except Exception as specialized_err:
            logger.error(f"Specialized analysis failed, using legacy composition: {specialized_err}")
            analysis = _compose_analysis_output(structured_analysis, analysis_obj, raw_video_text)
            detail_sections = {}
            specialized = None
        analysis += "\n\n" + format_priority_summary(priority)

        await set_progress(job_id, "FINALIZE")

        task_material = specialized.task_description if specialized else None
        extracted_tasks = extract_routed_tasks(task_material, url=url) if policy.create_tasks else []
        primary_title = extracted_tasks[0]['title'] if extracted_tasks else (
            specialized.what_it_is[:80].strip() if specialized and specialized.what_it_is else 'Интеграция решения из видео'
        )

        output_variants_payload = None
        output_variants_data = None
        canonical_result = None
        try:
            canonical_result = build_canonical_content_result(
                route=route,
                priority=priority,
                language_context=language_context,
                specialized=specialized,
                analysis=analysis_obj,
                raw_transcript=raw_video_text,
                title=primary_title,
                video_url=url,
            )
            output_variants_payload = generate_all_variants(canonical_result)
            output_variants_data = output_variants_payload.to_dict()
        except Exception as variants_err:
            logger.error(f"Output variants generation failed: {variants_err}")
            output_variants_data = None

        content_package = None
        if canonical_result and output_variants_payload:
            try:
                content_package = build_content_package(
                    job_id=job_id,
                    source_url=url,
                    canonical=canonical_result,
                    variants=output_variants_payload,
                    language_context=language_context.model_dump(),
                    router_decision=route.model_dump(),
                    priority_result=priority.model_dump(),
                )
            except Exception as pkg_err:
                logger.error(f"Failed to build ContentPackage for job {job_id}: {pkg_err}")

        user_delivery_text, output_variants_delivery, delivery_mode = resolve_telegram_delivery_payload(
            output_variants_payload, analysis
        )

        if qa_res is not None and not qa_res.approved:
            msg = "⚠️ Строгая проверка не пройдена. Автопубликация и создание задачи заблокированы.\nПричины:\n- " + "\n- ".join(qa_res.reasons or [])
            logger.warning(msg)
            if content_package:
                try:
                    transition_package_status(
                        content_package, PackageStatus.REVIEW_REQUIRED, "Strict QA gate failed"
                    )
                    await persist_content_package_models(content_package)
                except Exception as pkg_save_err:
                    logger.warning(f"Could not persist content_package on QA rejection: {pkg_save_err}")
            qa_reasons_data = {
                "analysis_json": analysis_obj.model_dump(),
                "language": language_context.model_dump(),
                "router": route.model_dump(),
                "priority": priority.model_dump(),
                "policy": combined_policy.model_dump(),
                "output_variants": output_variants_data,
                "content_package": {
                    "package_id": content_package.package_id,
                    "status": content_package.approval_state.value,
                    "contract_version": content_package.contract_version,
                } if content_package else None,
                "detail_sections": detail_sections,
                "audit_history": [],
            }
            await update_job_status(
                job_id, 'REVIEW_REQUIRED', error_text=msg,
                analysis_text=user_delivery_text, qa_reasons=qa_reasons_data,
            )
            await set_progress(
                job_id, "REVIEW_REQUIRED",
                reply_markup=analysis_keyboard(job_id, list(detail_sections)),
            )
            bot = Bot(token=settings.bot_token)
            try:
                await bot.send_message(chat_id=user_id, text=msg)
                await bot.send_message(
                    chat_id=user_id, text=user_delivery_text,
                    reply_markup=analysis_keyboard(job_id, list(detail_sections)),
                )
            except Exception:
                pass
            finally:
                await bot.session.close()
            return


        # Tasks are a routed downstream capability, not a universal side effect.
        delivery_status = {
            "language": language_delivery_outcome(language_context),
            "priority": priority_delivery_outcome(priority),
            "output_variants": output_variants_delivery,
            "content_package": "CREATED" if content_package else "FAILED",
            "user": "PENDING",
            "channel": "NOT_APPLICABLE",
            "plan": "NOT_APPLICABLE",
            "task_db": "NOT_APPLICABLE",
        }

        # Сохраняем разбор видео в Hermes plans (чистый бриф для архитектора)
        plan_file = f"/plans/idea_{job_id}.md"
        try:
            if not extracted_tasks:
                raise ValueError("No routed task to persist")
            with open(plan_file, "w", encoding="utf-8") as f:
                f.write(f"# {primary_title}\n\n")
                f.write(task_material or analysis)
                f.write("\n\n---\n\n")
                f.write(analysis)
            logger.info(f"Video idea brief saved to {plan_file} with title: {primary_title}")
            delivery_status["plan"] = "SUCCEEDED"
            
            # Добавляем в общий список (Backlog)
            backlog_file = "/plans/BACKLOG.md"
            with open(backlog_file, "a", encoding="utf-8") as bf:
                if os.path.getsize(backlog_file) == 0 if os.path.exists(backlog_file) else True:
                    bf.write("# База Идей (Backlog)\n\n")
                bf.writelines(f"- [ ] [{t['title']}]({plan_file.split('/')[-1]}) - {url}\n" for t in extracted_tasks)
                
        except ValueError:
            logger.info("Task/plan creation skipped by combined Router/priority policy.")
        except Exception as e:
            delivery_status["plan"] = f"FAILED:{type(e).__name__}"
            logger.error(f"Failed to save idea brief to {plan_file}: {e}")

        qa_reasons_data = {
            "analysis_json": analysis_obj.model_dump(),
            "mechanics_text": "### 📝 ДОСТУПНЫЙ МАТЕРИАЛ ВИДЕО\n" + raw_video_text[:1000] + "...",
            "language": language_context.model_dump(),
            **policy_observability_payload(route, combined_policy),
            "output_variants": output_variants_data,
            "content_package": {
                "package_id": content_package.package_id,
                "status": content_package.approval_state.value,
                "contract_version": content_package.contract_version,
            } if content_package else None,
            "specialized_analysis": specialized.model_dump() if specialized else None,
            "detail_sections": detail_sections,
            "delivery_status": delivery_status,
            "audit_history": [],
        }
        # Persist callback payload before the message containing its buttons is sent.
        await update_job_status(
            job_id, "PROCESSING", analysis_text=user_delivery_text, qa_reasons=qa_reasons_data
        )

        # Send result to user (Option A: Deliver TELEGRAM_LONG output variant)
        logger.info(f"Sending to TG user {user_id} via {delivery_mode}")
        from aiohttp import ClientTimeout
        session = AiohttpSession(timeout=ClientTimeout(total=900))
        bot = Bot(token=settings.bot_token, session=session)
        send_kwargs = {
            "chat_id": user_id,
            "video": FSInputFile(video_path),
        }
        if width > 0 and height > 0:
            send_kwargs["width"] = width
            send_kwargs["height"] = height
        try:
            msg = await bot.send_video(**send_kwargs)
            # Compact answer is one message; detailed layers stay behind callbacks.
            msg_user = await bot.send_message(
                chat_id=user_id,
                text=user_delivery_text,
                reply_markup=analysis_keyboard(job_id, list(detail_sections)),
            )
            delivery_status["user"] = "SUCCEEDED" if delivery_mode == "TELEGRAM_LONG" else "SUCCEEDED_FALLBACK"
            if content_package:
                tg_user_t = content_package.distribution_targets.get(TargetPlatform.TELEGRAM_USER.value)
                if tg_user_t:
                    tg_user_t.status = TargetDeliveryStatus.DELIVERED
                    tg_user_t.delivered_at = datetime.utcnow()
                    tg_user_t.external_id = str(msg_user.message_id)
                rec = DeliveryRecord.create(
                    package_id=content_package.package_id,
                    target=TargetPlatform.TELEGRAM_USER,
                    variant=OutputVariantType.TELEGRAM_LONG,
                    attempt_id=1,
                    approval_state=content_package.approval_state,
                    status=DeliveryOutcome.SUCCEEDED,
                    external_id=str(msg_user.message_id),
                )
                content_package.delivery_records.append(rec)
        except Exception as user_delivery_err:
            delivery_status["user"] = f"FAILED:{type(user_delivery_err).__name__}"
            await update_job_status(job_id, "PROCESSING", delivery_status=delivery_status)
            raise
        finally:
            await bot.session.close()
        
        # Publish to channel — idempotent: skip if this video was already published.
        channel_msg_id = None
        url_hash = None
        try:
            _, url_hash = clean_url(url)
        except Exception:
            url_hash = None
        already_published = False
        dedup_check_ok = True
        try:
            if combined_policy.channel_publication and url_hash:
                async with AsyncSessionLocal() as dup_s:
                    dup_job = (await dup_s.execute(
                        select(Job).where(
                            Job.url_hash == url_hash,
                            Job.tg_channel_message_id.isnot(None),
                        ).limit(1)
                    )).scalars().first()
                if dup_job:
                    already_published = True
                    logger.warning(
                        "DEDUP: url_hash %s already published as job_id=%s (first enqueued %s). Skipping channel post.",
                        url_hash, dup_job.id, dup_job.created_at,
                    )
        except Exception as dedup_err:
            dedup_check_ok = False
            delivery_status["channel"] = f"FAILED_DEDUP_CHECK:{type(dedup_err).__name__}"
            logger.error(f"Dedup check failed (non-blocking): {dedup_err}")

        if not combined_policy.channel_publication:
            delivery_status["channel"] = "SUPPRESSED_PRIORITY"
            logger.info(
                "Channel publication suppressed by priority gate (%s).",
                priority.decision,
            )
            if content_package:
                tg_ch_t = content_package.distribution_targets.get(TargetPlatform.TELEGRAM_CHANNEL.value)
                if tg_ch_t:
                    tg_ch_t.status = TargetDeliveryStatus.NOT_RENDERABLE
        elif already_published:
            delivery_status["channel"] = "SKIPPED_DUPLICATE"
            logger.info("Skipping channel publish (duplicate video). User already received the analysis above.")
            if content_package:
                tg_ch_t = content_package.distribution_targets.get(TargetPlatform.TELEGRAM_CHANNEL.value)
                if tg_ch_t:
                    tg_ch_t.status = TargetDeliveryStatus.SKIPPED_DUPLICATE
                rec = DeliveryRecord.create(
                    package_id=content_package.package_id,
                    target=TargetPlatform.TELEGRAM_CHANNEL,
                    variant=OutputVariantType.TELEGRAM_LONG,
                    attempt_id=1,
                    approval_state=content_package.approval_state,
                    status=DeliveryOutcome.SKIPPED_DUPLICATE,
                )
                content_package.delivery_records.append(rec)
        elif dedup_check_ok:
            channel_bot = None
            try:
                channel_id = settings.channel_chat_id or "@savemyreels"
                logger.info(f"Publishing to channel {channel_id}")
                channel_session = AiohttpSession(timeout=ClientTimeout(total=900))
                channel_bot = Bot(token=settings.bot_token, session=channel_session)
                channel_kwargs = {
                    "chat_id": channel_id,
                    "video": FSInputFile(video_path),
                }
                if width > 0 and height > 0:
                    channel_kwargs["width"] = width
                    channel_kwargs["height"] = height
                ch_msg = await channel_bot.send_video(**channel_kwargs)
                channel_msg_id = ch_msg.message_id
                summary = _extract_summary(analysis)
                await send_long_text(channel_bot, channel_id, summary)
                delivery_status["channel"] = "SUCCEEDED"
                logger.info(f"Published to channel successfully. msg_id={channel_msg_id}")
                if content_package:
                    tg_ch_t = content_package.distribution_targets.get(TargetPlatform.TELEGRAM_CHANNEL.value)
                    if tg_ch_t:
                        tg_ch_t.status = TargetDeliveryStatus.DELIVERED
                        tg_ch_t.delivered_at = datetime.utcnow()
                        tg_ch_t.external_id = str(channel_msg_id)
                    rec = DeliveryRecord.create(
                        package_id=content_package.package_id,
                        target=TargetPlatform.TELEGRAM_CHANNEL,
                        variant=OutputVariantType.TELEGRAM_LONG,
                        attempt_id=1,
                        approval_state=content_package.approval_state,
                        status=DeliveryOutcome.SUCCEEDED,
                        external_id=str(channel_msg_id),
                    )
                    content_package.delivery_records.append(rec)
            except Exception as e:
                delivery_status["channel"] = f"FAILED:{type(e).__name__}"
                logger.error(f"Channel publish failed: {e}")
                if content_package:
                    tg_ch_t = content_package.distribution_targets.get(TargetPlatform.TELEGRAM_CHANNEL.value)
                    if tg_ch_t:
                        tg_ch_t.status = TargetDeliveryStatus.FAILED
                        tg_ch_t.error_message = str(e)
                    rec = DeliveryRecord.create(
                        package_id=content_package.package_id,
                        target=TargetPlatform.TELEGRAM_CHANNEL,
                        variant=OutputVariantType.TELEGRAM_LONG,
                        attempt_id=1,
                        approval_state=content_package.approval_state,
                        status=DeliveryOutcome.FAILED,
                        error_message=str(e),
                    )
                    content_package.delivery_records.append(rec)
            finally:
                if channel_bot is not None:
                    await channel_bot.session.close()

        # Only set tg_channel_message_id when we actually published; on a dedup
        # skip, leave the existing marker intact so idempotency holds across runs.
        done_kwargs = {
            "tg_file_id": msg.video.file_id,
            "analysis_text": user_delivery_text,
            "qa_reasons": qa_reasons_data,
            **deferred_audit_fields(),
        }
        if channel_msg_id:
            done_kwargs["tg_channel_message_id"] = channel_msg_id
        # Сохраняем задачи в базу данных (PostgreSQL)
        try:
            if extracted_tasks:
                async with AsyncSessionLocal() as session:
                    for t in extracted_tasks:
                        new_task = Task(
                            id=str(uuid.uuid4()),
                            job_id=job_id,
                            user_id=user_id,
                            title=t['title'],
                            description=t.get('description'),
                            status='PENDING',
                        )
                        session.add(new_task)
                    await session.commit()
                logger.info(f"Tasks saved to DB: {[t['title'] for t in extracted_tasks]}")
                delivery_status["task_db"] = "SUCCEEDED"
        except Exception as e:
            delivery_status["task_db"] = f"FAILED:{type(e).__name__}"
            logger.error(f"Failed to save task to DB: {e}")

        if content_package:
            try:
                transition_package_status(
                    content_package,
                    PackageStatus.PARTIALLY_DELIVERED,
                    "Telegram delivered, external platforms pending review",
                )
            except Exception:
                pass
            await persist_content_package_models(content_package)

        qa_reasons_data["delivery_status"] = delivery_status
        done_kwargs["qa_reasons"] = qa_reasons_data
        done_kwargs["delivery_status"] = delivery_status
        completion_status = determine_completion_status(delivery_status)
        if completion_status == "PARTIAL":
            failed_steps = [
                name for name, outcome in delivery_status.items()
                if outcome.startswith("FAILED")
            ]
            done_kwargs["error_text"] = "Partial delivery failure: " + ", ".join(failed_steps)
        else:
            done_kwargs["error_text"] = None
        await update_job_status(job_id, completion_status, **done_kwargs)
        await set_progress(
            job_id, "PARTIAL" if completion_status == "PARTIAL" else "COMPLETE",
            reply_markup=analysis_keyboard(job_id, list(detail_sections)),
        )
        if completion_status == "PARTIAL":
            warning_bot = Bot(token=settings.bot_token)
            try:
                await warning_bot.send_message(
                    chat_id=user_id,
                    text="⚠️ Анализ готов, но часть delivery-шагов требует проверки.",
                )
            except Exception:
                logger.exception("Could not send partial-delivery warning for job %s", job_id)
            finally:
                await warning_bot.session.close()

        logger.info(f"Job {job_id} completed with status {completion_status}.")
        
    except asyncio.CancelledError:
        # ARQ cancels this coroutine when job_timeout is reached (or the worker
        # shuts down). CancelledError is a BaseException since 3.8, so
        # `except Exception` never sees it — which is exactly how jobs used to
        # freeze in PROCESSING forever: no status, no reason, no message to the
        # user, and the reaper had to clean up after the fact.
        reason = (
            "Job cancelled before completion (ARQ job_timeout or worker "
            f"shutdown) after {time.monotonic() - started_monotonic:.0f}s"
        )
        logger.error(f"Job {job_id} CANCELLED: {reason}")
        try:
            await update_job_status(job_id, 'ERROR', error_text=reason)
            await set_progress(job_id, "ERROR")
        except Exception as write_err:
            logger.error(f"Job {job_id}: could not persist cancellation reason: {write_err}")
        raise
    except Exception as e:
        logger.error(f"Job {job_id} failed: {e}")
        error_kwargs = {"error_text": format_error_text(e)}
        if "delivery_status" in locals():
            error_kwargs["delivery_status"] = delivery_status
        await update_job_status(job_id, 'ERROR', **error_kwargs)
        await set_progress(job_id, "ERROR")
        bot = Bot(token=settings.bot_token)
        try:
            await bot.send_message(chat_id=user_id, text=f"Произошла ошибка при обработке видео: {e}")
        except Exception:
            pass
        finally:
            await bot.session.close()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
