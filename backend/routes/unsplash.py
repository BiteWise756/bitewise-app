"""Unsplash image search routes.

Provides endpoints for searching food images via the Unsplash API.
Results are cached server-side to minimize API calls.

Endpoints:
  GET /unsplash/search?name=<food_name>  — Search for a single food image
  POST /unsplash/search-batch            — Search for multiple food images at once
"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import List, Optional

from services.unsplash_service import search_food_image, search_food_images_batch

router = APIRouter(prefix="/unsplash", tags=["Unsplash"])


class BatchSearchRequest(BaseModel):
    names: List[str]


@router.get("/search")
async def search_image(name: str = Query(..., description="Food/meal name to search")):
    """Search Unsplash for a food photo matching the given name.

    Returns the photo URL and attribution information from the Unsplash API.
    Results are cached server-side for 24 hours.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="Name parameter is required")

    result = search_food_image(name.strip())
    return {"success": True, "data": result}


@router.post("/search-batch")
async def search_images_batch(request: BatchSearchRequest):
    """Search Unsplash for multiple food names at once.

    Returns a dictionary mapping each name to its search result.
    Useful for loading Today's Meals images in a single request.
    """
    if not request.names:
        raise HTTPException(status_code=400, detail="Names list is required")

    # Limit to 20 names per batch to prevent abuse
    names = request.names[:20]
    results = search_food_images_batch(names)
    return {"success": True, "data": results}
