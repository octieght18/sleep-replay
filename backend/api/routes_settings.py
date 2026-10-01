from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/api/settings")
def get_settings(request: Request):
    return request.app.state.settings.get().to_dict()


@router.put("/api/settings")
def put_settings(request: Request, body: dict):
    return request.app.state.settings.put(body).to_dict()
