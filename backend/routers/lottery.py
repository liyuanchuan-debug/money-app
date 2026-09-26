from fastapi import APIRouter, HTTPException, Query

from models.lottery import (
    RecommendRequest,
    RecordIn,
    RecordOut,
    SettingsOut,
    SettingsPatch,
)
from repository import get_store
from services.lottery import recommend

router = APIRouter(prefix="/api", tags=["lottery"])


@router.get("/records", response_model=list[RecordOut])
async def list_records(limit: int | None = Query(default=None, ge=1, le=500)):
    store = await get_store()
    return await store.list_records(limit)


@router.post("/records", response_model=RecordOut, status_code=201)
async def add_record(payload: RecordIn):
    store = await get_store()
    record = await store.add_record(payload.number)
    return record


@router.delete("/records")
async def clear_records():
    store = await get_store()
    deleted = await store.clear_records()
    return {"deleted": deleted}


@router.get("/settings", response_model=SettingsOut)
async def get_settings():
    store = await get_store()
    return await store.get_settings()


@router.put("/settings", response_model=SettingsOut)
async def update_settings(payload: SettingsPatch):
    store = await get_store()
    return await store.update_settings(payload.model_dump())


@router.post("/recommend")
async def post_recommend(payload: RecommendRequest | None = None):
    payload = payload or RecommendRequest()
    store = await get_store()

    if payload.number is not None:
        latest = payload.number
        recent = await store.list_records(limit=2)
        previous = recent[0]["number"] if recent else None
        history = [r["number"] for r in await store.list_records()]
    else:
        recent = await store.list_records(limit=2)
        if not recent:
            raise HTTPException(status_code=400, detail="暂无开奖记录，请先输入最新开奖号")
        latest = recent[0]["number"]
        previous = recent[1]["number"] if len(recent) > 1 else None
        history = [r["number"] for r in await store.list_records()]

    return recommend(
        latest=latest,
        previous=previous,
        history_numbers=history,
        settings=await store.get_settings(),
        mode=payload.mode,
    )


@router.get("/export")
async def export_data():
    store = await get_store()
    data = await store.export_data()
    data["storage"] = store.backend
    return data


@router.post("/import")
async def import_data(
    payload: dict, replace: bool = Query(default=True)
):
    store = await get_store()
    records = payload.get("records")
    if records is None or not isinstance(records, list):
        raise HTTPException(status_code=422, detail="JSON 缺少 records 数组")

    for item in records:
        try:
            number = int(item["number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"非法记录: {item!r}") from exc
        if not 1 <= number <= 49:
            raise HTTPException(status_code=422, detail=f"号码越界: {number}")

    return await store.import_data(payload, replace=replace)
