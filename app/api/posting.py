from fastapi import APIRouter, Request

from app.schemas import PostingRequest, PostingResponse

router = APIRouter()


@router.post(
    "/posting",
    response_model=PostingResponse,
    summary="Publish a short message to every configured platform",
)
async def create_posting(payload: PostingRequest, request: Request) -> PostingResponse:
    service = request.app.state.posting_service
    return await service.publish(payload.message, payload.cta_url)
