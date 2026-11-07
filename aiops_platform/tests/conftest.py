import asyncio

import pytest
import pytest_asyncio

from aiops_platform.context.objects import InMemoryObjectStore
from aiops_platform.context.service import ContextService, Principal
from aiops_platform.context.store import ContextStore
from aiops_platform.contracts.enums import Environment, Priority, Severity
from aiops_platform.contracts.events import ServiceIdentity


@pytest_asyncio.fixture
async def store(tmp_path):
    store = await ContextStore.connect(
        f"sqlite+aiosqlite:///{tmp_path / 'context.db'}", InMemoryObjectStore()
    )
    yield store
    await store.close()


@pytest.fixture
def context(store):
    return ContextService(store)


@pytest.fixture
def orchestrator():
    return Principal.orchestrator()


SERVICE = ServiceIdentity(service_id="payment-service", namespace="prod", version="v2.7")


async def open_incident(context, incident_id="INC-TEST-0001", environment=Environment.PRODUCTION):
    ctx, _ = await context.open_incident(
        Principal.orchestrator(),
        incident_id=incident_id,
        kind="INCIDENT",
        service=SERVICE,
        environment=environment,
        severity=Severity.HIGH,
        priority=Priority.P1,
        signals={"memory_pct": 94.0},
    )
    return ctx


@pytest.fixture
def no_sleep():
    async def sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    return sleep
