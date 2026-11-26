from types import ModuleType

from fastapi import APIRouter


def create_api_router(handlers: ModuleType) -> APIRouter:
    router = APIRouter()
    routes = (
        ("/", "read_root", ["GET"], "general", None),
        ("/ui", "optimizer_interface", ["GET"], "general", None),
        ("/auth/config", "get_auth_config", ["GET"], "auth", None),
        ("/auth/register", "register_account", ["POST"], "auth", 202),
        ("/auth/recovery", "request_password_reset", ["POST"], "auth", 202),
        ("/auth/password-reset", "reset_account_password", ["POST"], "auth", None),
        ("/auth/verify", "verify_account", ["POST"], "auth", None),
        ("/auth/login", "login_account", ["POST"], "auth", None),
        ("/auth/me", "current_account", ["GET"], "auth", None),
        ("/auth/logout", "logout_account", ["POST"], "auth", None),
        ("/account/mt5", "save_mt5_credentials", ["PUT"], "account", None),
        ("/system/status", "get_system_status", ["GET"], "system", None),
        ("/connection-check", "submit_connection_check", ["POST"], "jobs", 202),
        ("/optimize", "optimize_uploaded_set", ["POST"], "optimization", 202),
        ("/jobs", "list_optimization_jobs", ["GET"], "jobs", None),
        ("/jobs/{job_id}", "get_optimization_job", ["GET"], "jobs", None),
        ("/results/{filename}", "download_best_set_file", ["GET"], "results", None),
        ("/reports/{filename}", "download_excel_report", ["GET"], "results", None),
    )
    for path, handler_name, methods, tag, status_code in routes:
        endpoint = getattr(handlers, handler_name)
        if status_code is None:
            router.add_api_route(
                path,
                endpoint,
                methods=methods,
                tags=[tag],
                name=handler_name,
            )
        else:
            router.add_api_route(
                path,
                endpoint,
                methods=methods,
                status_code=status_code,
                tags=[tag],
                name=handler_name,
            )
    return router
