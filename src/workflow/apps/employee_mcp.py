from workflow.apps.mcp_asgi import build_authenticated_app
from workflow.apps.mcp_factory import create_mcp_server
from workflow.config import get_settings

settings = get_settings()
mcp = create_mcp_server(settings, "EMPLOYEE")
app = build_authenticated_app(mcp, settings, "EMPLOYEE")
