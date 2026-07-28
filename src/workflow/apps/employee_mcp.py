from workflow.apps.mcp_asgi import build_authenticated_app
from workflow.apps.mcp_factory import create_mcp_server
from workflow.config import get_settings
from workflow.logging import configure_logging

configure_logging("employee-mcp")
settings = get_settings()
mcp = create_mcp_server(settings, "EMPLOYEE")
app = build_authenticated_app(mcp, settings, "EMPLOYEE")
