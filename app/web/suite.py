"""JSON the web UI needs beyond the validation API: the model catalog. Same sign-in as the API
(app/web/validations.require_key), but not part of /openapi.json."""
from fastapi import APIRouter, Depends

from app import registry
from app.web.public import require_key

api = APIRouter(prefix="/api/suite", dependencies=[Depends(require_key)], include_in_schema=False)


@api.get("/models")
def models():
    """The model catalog (config/models.json) consolidated with what the gateway serves."""
    return registry.report()


@api.get("/migrations/{provider}")
def migrations(provider: str):
    """The vendor's recommended migration paths for a provider (config/migrations/<provider>.json)."""
    from app.migration import migration_paths
    return [p for p in migration_paths() if (p.get("provider") or "").lower() == provider.lower()]
