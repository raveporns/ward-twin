
import csv
import io

from fastapi import APIRouter, HTTPException, Depends
from fastapi.responses import Response

from app.dependencies.auth import get_current_user

router = APIRouter(tags=["Import Templates"])

TEMPLATES = {
    "encounters": [
        "patient_id",
        "hn",
        "display_name",
        "birth_date",
        "sex",
        "encounter_id",
        "an",
        "ward_id",
        "bed_label",
        "admitted_at",
    ]
}


@router.get("/import-templates/{kind}")
def download_import_template(
    kind: str,
    user: dict = Depends(get_current_user),
):
    if kind not in TEMPLATES:
        raise HTTPException(
            status_code=404,
            detail="Import template not found",
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(TEMPLATES[kind])

    csv_content = "\ufeff" + output.getvalue()

    return Response(
        content=csv_content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition":
                f'attachment; filename="{kind}_template.csv"'
        },
    )
