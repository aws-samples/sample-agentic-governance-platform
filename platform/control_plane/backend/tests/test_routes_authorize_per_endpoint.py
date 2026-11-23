"""No router and no route of the built app carries a dependency: authorization is per endpoint.

The rule is read off the app FastAPI serves, so a dependency given at include time, appended to a
router after construction, spread in from a mapping or attached to a nested include is seen. Not
held: a router included from inside a startup hook, because the walk reads the app as imported.
"""

from typing import Any

import pytest
from fastapi.routing import APIRoute, _IncludedRouter
from starlette.routing import BaseRoute, Route

_MISSING = object()


def _required(owner: object, name: str, label: str) -> Any:
    value = getattr(owner, name, _MISSING)
    if value is _MISSING:
        pytest.fail(
            f"{label}: {type(owner).__qualname__} has no {name!r}, so its dependencies cannot be read"
        )
    return value


def _walk(routes: list[BaseRoute], prefix: str, findings: list[str], exempt: set[str]) -> int:
    reached = 0
    for route in routes:
        if isinstance(route, _IncludedRouter):
            context = _required(route, "include_context", f"a router included under {prefix or '/'}")
            router = _required(route, "original_router", f"a router included under {prefix or '/'}")
            at = prefix + _required(context, "prefix", f"a router included under {prefix or '/'}")
            path = at + _required(router, "prefix", f"a router included under {at or '/'}")
            label = f"the router at {path or '/'}"
            if _required(context, "dependencies", label) != []:
                findings.append(f"{label}: include_router(dependencies={context.dependencies!r})")
            if _required(router, "dependencies", label) != []:
                findings.append(f"{label}: APIRouter.dependencies {router.dependencies!r}")
            reached += _walk(_required(router, "routes", label), at, findings, exempt)
        elif isinstance(route, APIRoute):
            reached += 1
            label = f"{sorted(route.methods or ())} {prefix}{route.path}"
            if _required(route, "dependencies", label) != []:
                findings.append(f"{label}: route dependencies {route.dependencies!r}")
            dependant = _required(route, "dependant", label)
            findings.extend(
                f"{label}: a parameterless dependency FastAPI runs, {dependency.call!r}"
                for dependency in _required(dependant, "dependencies", label)
                if _required(dependency, "name", label) is None
            )
        elif type(route) is Route:
            if prefix + route.path not in exempt:
                findings.append(
                    f"{sorted(route.methods or ())} {prefix}{route.path}: a plain Route cannot carry"
                    " RBACDepends"
                )
        else:
            findings.append(
                f"{type(route).__qualname__} under {prefix or '/'}: its dependencies are not read"
            )
    return reached


def test_no_router_and_no_route_carries_a_dependency():
    import main

    app = main.app
    exempt = {
        url
        for url in (app.openapi_url, app.docs_url, app.redoc_url, app.swagger_ui_oauth2_redirect_url)
        if url
    }
    findings: list[str] = []
    hollow: list[str] = []
    if app.router.dependencies != []:
        findings.append(f"the app: FastAPI(dependencies={app.router.dependencies!r})")
    for route in app.routes:
        if _walk([route], "", findings, exempt) == 0 and isinstance(route, _IncludedRouter):
            hollow.append(
                f"the router at {route.include_context.prefix + route.original_router.prefix or '/'}"
            )

    assert hollow == [], (
        "the walk reached no APIRoute under these routers, so it proved nothing:\n" + "\n".join(hollow)
    )
    assert findings == [], (
        "Authorization is per endpoint: gate each route with a trailing RBACDepends parameter, never a"
        " dependency on the app, a router, an include or a route decorator:\n" + "\n".join(findings)
    )


def test_every_registered_router_is_walked():
    import main

    assert main.settings.ROOT_PATH == "", (
        f"ROOT_PATH is {main.settings.ROOT_PATH!r}: main.py registers every router a second time under it"
    )
    included = sum(isinstance(route, _IncludedRouter) for route in main.app.routes)
    registered = len(main.API_ROUTERS)
    assert included == registered + 1, (
        f"the app holds {included} included routers, main.py registers {registered} API routers and the"
        " health router"
    )
