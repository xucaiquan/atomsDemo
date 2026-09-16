import json
import logging
from typing import List, Optional

from datetime import datetime, date

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from core.database import get_db
from services.versions import VersionsService

# Set up logging
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/entities/versions", tags=["versions"])


# ---------- Pydantic Schemas ----------
class VersionsData(BaseModel):
    """Entity data schema (for create/update)"""
    project_public_id: str
    seq: int
    prompt: str
    html: str = None
    summary: str = None
    status: str
    error: str = None
    duration_ms: int = None


class VersionsUpdateData(BaseModel):
    """Update entity data (partial updates allowed)"""
    project_public_id: Optional[str] = None
    seq: Optional[int] = None
    prompt: Optional[str] = None
    html: Optional[str] = None
    summary: Optional[str] = None
    status: Optional[str] = None
    error: Optional[str] = None
    duration_ms: Optional[int] = None


class VersionsResponse(BaseModel):
    """Entity response schema"""
    id: int
    project_public_id: str
    seq: int
    prompt: str
    html: Optional[str] = None
    summary: Optional[str] = None
    status: str
    error: Optional[str] = None
    duration_ms: Optional[int] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class VersionsListResponse(BaseModel):
    """List response schema"""
    items: List[VersionsResponse]
    total: int
    skip: int
    limit: int


class VersionsBatchCreateRequest(BaseModel):
    """Batch create request"""
    items: List[VersionsData]


class VersionsBatchUpdateItem(BaseModel):
    """Batch update item"""
    id: int
    updates: VersionsUpdateData


class VersionsBatchUpdateRequest(BaseModel):
    """Batch update request"""
    items: List[VersionsBatchUpdateItem]


class VersionsBatchDeleteRequest(BaseModel):
    """Batch delete request"""
    ids: List[int]


# ---------- Routes ----------
@router.get("", response_model=VersionsListResponse)
async def query_versionss(
    query: str = Query(None, description='Query conditions as JSON, e.g. {"id":2} or {"id":{"$gte":2}}'),
    sort: str = Query(None, description="Sort field (prefix with '-' for descending)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(20, ge=1, le=2000, description="Max number of records to return"),
    fields: str = Query(None, description="Comma-separated list of fields to return"),
    db: AsyncSession = Depends(get_db),
):
    """Query versionss with filtering, sorting, and pagination"""
    logger.debug(f"Querying versionss: query={query}, sort={sort}, skip={skip}, limit={limit}, fields={fields}")
    
    service = VersionsService(db)
    try:
        # Parse query JSON if provided
        query_dict = None
        if query:
            try:
                query_dict = json.loads(query)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid query JSON format")
        
        result = await service.get_list(
            skip=skip, 
            limit=limit,
            query_dict=query_dict,
            sort=sort,
        )
        logger.debug(f"Found {result['total']} versionss")
        return result
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Invalid versions query: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error querying versionss: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("/all", response_model=VersionsListResponse)
async def query_versionss_all(
    query: str = Query(None, description='Query conditions as JSON, e.g. {"id":2} or {"id":{"$gte":2}}'),
    sort: str = Query(None, description="Sort field (prefix with '-' for descending)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(20, ge=1, le=2000, description="Max number of records to return"),
    fields: str = Query(None, description="Comma-separated list of fields to return"),
    db: AsyncSession = Depends(get_db),
):
    # Query versionss with filtering, sorting, and pagination without user limitation
    logger.debug(f"Querying versionss: query={query}, sort={sort}, skip={skip}, limit={limit}, fields={fields}")

    service = VersionsService(db)
    try:
        # Parse query JSON if provided
        query_dict = None
        if query:
            try:
                query_dict = json.loads(query)
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid query JSON format")

        result = await service.get_list(
            skip=skip,
            limit=limit,
            query_dict=query_dict,
            sort=sort
        )
        logger.debug(f"Found {result['total']} versionss")
        return result
    except HTTPException:
        raise
    except ValueError as e:
        logger.warning(f"Invalid versions query: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error querying versionss: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.get("/{id}", response_model=VersionsResponse)
async def get_versions(
    id: int,
    fields: str = Query(None, description="Comma-separated list of fields to return"),
    db: AsyncSession = Depends(get_db),
):
    """Get a single versions by ID"""
    logger.debug(f"Fetching versions with id: {id}, fields={fields}")
    
    service = VersionsService(db)
    try:
        result = await service.get_by_id(id)
        if not result:
            logger.warning(f"Versions with id {id} not found")
            raise HTTPException(status_code=404, detail="Versions not found")
        
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error fetching versions {id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("", response_model=VersionsResponse, status_code=201)
async def create_versions(
    data: VersionsData,
    db: AsyncSession = Depends(get_db),
):
    """Create a new versions"""
    logger.debug(f"Creating new versions with data: {data}")
    
    service = VersionsService(db)
    try:
        result = await service.create(data.model_dump())
        if not result:
            raise HTTPException(status_code=400, detail="Failed to create versions")
        
        logger.info(f"Versions created successfully with id: {result.id}")
        return result
    except ValueError as e:
        logger.error(f"Validation error creating versions: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error creating versions: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.post("/batch", response_model=List[VersionsResponse], status_code=201)
async def create_versionss_batch(
    request: VersionsBatchCreateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Create multiple versionss in a single request"""
    logger.debug(f"Batch creating {len(request.items)} versionss")
    
    service = VersionsService(db)
    results = []
    
    try:
        for item_data in request.items:
            result = await service.create(item_data.model_dump())
            if result:
                results.append(result)
        
        logger.info(f"Batch created {len(results)} versionss successfully")
        return results
    except Exception as e:
        await db.rollback()
        logger.error(f"Error in batch create: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Batch create failed: {str(e)}")


@router.put("/batch", response_model=List[VersionsResponse])
async def update_versionss_batch(
    request: VersionsBatchUpdateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Update multiple versionss in a single request"""
    logger.debug(f"Batch updating {len(request.items)} versionss")
    
    service = VersionsService(db)
    results = []
    
    try:
        for item in request.items:
            # Only include non-None values for partial updates
            update_dict = {k: v for k, v in item.updates.model_dump().items() if v is not None}
            result = await service.update(item.id, update_dict)
            if result:
                results.append(result)
        
        logger.info(f"Batch updated {len(results)} versionss successfully")
        return results
    except Exception as e:
        await db.rollback()
        logger.error(f"Error in batch update: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Batch update failed: {str(e)}")


@router.put("/{id}", response_model=VersionsResponse)
async def update_versions(
    id: int,
    data: VersionsUpdateData,
    db: AsyncSession = Depends(get_db),
):
    """Update an existing versions"""
    logger.debug(f"Updating versions {id} with data: {data}")

    service = VersionsService(db)
    try:
        # Only include non-None values for partial updates
        update_dict = {k: v for k, v in data.model_dump().items() if v is not None}
        result = await service.update(id, update_dict)
        if not result:
            logger.warning(f"Versions with id {id} not found for update")
            raise HTTPException(status_code=404, detail="Versions not found")
        
        logger.info(f"Versions {id} updated successfully")
        return result
    except HTTPException:
        raise
    except ValueError as e:
        logger.error(f"Validation error updating versions {id}: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Error updating versions {id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@router.delete("/batch")
async def delete_versionss_batch(
    request: VersionsBatchDeleteRequest,
    db: AsyncSession = Depends(get_db),
):
    """Delete multiple versionss by their IDs"""
    logger.debug(f"Batch deleting {len(request.ids)} versionss")
    
    service = VersionsService(db)
    deleted_count = 0
    
    try:
        for item_id in request.ids:
            success = await service.delete(item_id)
            if success:
                deleted_count += 1
        
        logger.info(f"Batch deleted {deleted_count} versionss successfully")
        return {"message": f"Successfully deleted {deleted_count} versionss", "deleted_count": deleted_count}
    except Exception as e:
        await db.rollback()
        logger.error(f"Error in batch delete: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Batch delete failed: {str(e)}")


@router.delete("/{id}")
async def delete_versions(
    id: int,
    db: AsyncSession = Depends(get_db),
):
    """Delete a single versions by ID"""
    logger.debug(f"Deleting versions with id: {id}")
    
    service = VersionsService(db)
    try:
        success = await service.delete(id)
        if not success:
            logger.warning(f"Versions with id {id} not found for deletion")
            raise HTTPException(status_code=404, detail="Versions not found")
        
        logger.info(f"Versions {id} deleted successfully")
        return {"message": "Versions deleted successfully", "id": id}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error deleting versions {id}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")