
import csv
import io
from enum import Enum

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import Response

from app.dependencies.auth import get_current_user


router = APIRouter(tags=["Import Templates"])


# ============================================================
# Supported Template Types
# ============================================================

class ImportTemplateKind(str, Enum):
    encounters = "encounters"


TEMPLATES = {
    ImportTemplateKind.encounters: [
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
    ],
}


# ============================================================
# Download CSV Template
# ============================================================

@router.get(
    "/import-templates/{kind}",
    summary="Download CSV Import Template",
    description=(
        "ดาวน์โหลดไฟล์ CSV Template สำหรับนำเข้าข้อมูล "
        "ปัจจุบันรองรับประเภท encounters เท่านั้น "
        "ผู้ใช้ต้องเข้าสู่ระบบด้วย Bearer JWT"
    ),
    response_class=Response,
    responses={
        200: {
            "description": "CSV template downloaded successfully",
            "content": {
                "text/csv": {
                    "schema": {
                        "type": "string",
                        "format": "binary",
                    }
                }
            },
        },
        401: {
            "description": "Authentication required",
        },
        403: {
            "description": "User account inactive or access denied",
        },
        422: {
            "description": "Unsupported template kind",
        },
    },
)
def download_import_template(
    kind: ImportTemplateKind,
    user: dict = Depends(get_current_user),
):
    columns = TEMPLATES.get(kind)

    if columns is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Import template not found",
        )

    # Create CSV in memory
    output = io.StringIO(newline="")

    writer = csv.writer(
        output,
        lineterminator="\r\n",
    )

    writer.writerow(columns)

    # UTF-8 BOM for Excel compatibility
    csv_content = "\ufeff" + output.getvalue()

    filename = f"{kind.value}_template.csv"

    return Response(
        content=csv_content.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"'
            ),
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )
