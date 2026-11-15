"""Repair saved back links from retained source evidence; dry-run unless --apply is supplied."""
import argparse
import json
from datetime import UTC, datetime

from sqlalchemy import exists, select, update

from core.errors import StageError
from db.models import Image, Record, Task
from db.session import Session
from integrations.yadvashem import normalized_images, resolve_image, source_back_reference
from services.ingestion.jobs import add_task, current_job


def repair_back_images(*, apply=False):
    corrections = []
    with Session.begin() as session:
        job = current_job(session)
        if apply and (job.status != "paused" or session.scalar(select(exists().where(
                Task.job_id == job.id, Task.status == "running")))):
            raise StageError("job_transition")
        rows = session.execute(select(Image, Record).join(Record, Record.id == Image.record_id).where(
            Image.active, Image.back_reference.is_not(None), Record.source == "live",
        ).order_by(Record.photo_id, Image.id)).all()
        for image, record in rows:
            detail_images = normalized_images(record.raw_details.get("images") or [], record.source)
            corrected = source_back_reference(image.back_reference, detail_images, record.source)
            if corrected == image.back_reference:
                continue
            paired = normalized_images([{"url": image.front_reference, "backImage": corrected}], record.source)[0]
            reverse_path = resolve_image(corrected, record.source)[0]
            siblings = session.scalars(select(Image).where(
                Image.record_id == record.id, Image.id != image.id, Image.active,
                Image.back_reference.is_(None),
            )).all()
            # Restored rows can retain identities from an older normalizer. Match
            # their actual canonical source references rather than the stored hash.
            duplicates = [item for item in siblings
                          if (resolved := resolve_image(item.front_reference, record.source))
                          and resolved[0] == reverse_path]
            corrections.append({"photo_id": record.photo_id, "image_id": image.id,
                                "old_back_reference": image.back_reference, "new_back_reference": corrected,
                                "duplicate_image_ids": [item.id for item in duplicates]})
            if not apply:
                continue
            for key in ("back_reference", "back_url", "reference_fingerprint"):
                setattr(image, key, paired[key])
            image.status = "pending"
            for duplicate in duplicates:
                duplicate.active = False
                # Preserve successful checkpoints and historical attempts. Unfinished
                # work for the separate reverse is superseded by the paired image.
                session.execute(update(Task).where(
                    Task.job_id == job.id, Task.image_id == duplicate.id,
                    Task.status.in_(("queued", "failed", "blocked", "interrupted")),
                ).values(status="skipped", error_code=None, claim_token=None))
            add_task(session, job.id, "vision", f"vision:{image.id}", photo_id=record.photo_id, image_id=image.id)
            add_task(session, job.id, "save", f"save:{image.id}", photo_id=record.photo_id, image_id=image.id)
            session.execute(update(Task).where(
                Task.job_id == job.id, Task.image_id == image.id,
                Task.stage.in_(("vision", "vision_embedding", "save")),
            ).values(status="queued", error_code=None, claim_token=None, available_at=datetime.now(UTC)))
    return corrections


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    changes = repair_back_images(apply=args.apply)
    print(json.dumps({"applied": args.apply, "count": len(changes), "corrections": changes}, ensure_ascii=False))
