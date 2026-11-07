"""The simulated cluster as a network service, for the distributed compose stack.

`create_cluster_app()` serves one `SimulatedCluster` (Kubernetes state +
telemetry + fault injection) over HTTP; a background clock advances simulated
time. `RemoteCluster` is a `SimulatedCluster` whose state lives on that
server: reads fetch the deployment, writes are POSTs. The telemetry model
runs client-side on the fetched state, so signals are identical everywhere.
"""

import asyncio
import threading
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent4_deployment_recovery.core.errors import KubernetesAdapterError
from agent4_deployment_recovery.core.kubernetes_models import JsonPatchOperation
from aiops_platform.sim.cluster import Release, SimDeployment, SimulatedCluster
from aiops_platform.sim.scenarios import base_cluster, cpu_load_spike, memory_leak_after_deploy


def deployment_json(dep: SimDeployment) -> dict[str, Any]:
    return {
        "service_id": dep.service_id,
        "namespace": dep.namespace,
        "container": dep.container,
        "releases": [
            {"version": r.version, "image": r.image, "faults": sorted(r.faults)} for r in dep.releases.values()
        ],
        "image": dep.image,
        "replicas": dep.replicas,
        "memory_limit_mib": dep.memory_limit_mib,
        "rps": dep.rps,
        "uid": dep.uid,
        "resource_version": dep.resource_version,
        "generation": dep.generation,
        "deployed_at_min": dep.deployed_at_min,
        "history": [[m, v] for m, v in dep.history],
        "restarts": dep.restarts,
    }


def deployment_from_json(data: dict[str, Any]) -> SimDeployment:
    releases = {r["version"]: Release(r["version"], r["image"], frozenset(r["faults"])) for r in data["releases"]}
    return SimDeployment(
        service_id=data["service_id"],
        namespace=data["namespace"],
        container=data["container"],
        releases=releases,
        image=data["image"],
        replicas=data["replicas"],
        memory_limit_mib=data["memory_limit_mib"],
        rps=data["rps"],
        uid=data["uid"],
        resource_version=data["resource_version"],
        generation=data["generation"],
        deployed_at_min=data["deployed_at_min"],
        history=[(m, v) for m, v in data["history"]],
        restarts=data["restarts"],
    )


class PatchBody(BaseModel):
    patch: list[dict[str, Any]]


def create_cluster_app(cluster: SimulatedCluster | None = None, *, seconds_per_minute: float = 10.0) -> FastAPI:
    app = FastAPI(title="Simulated cluster")
    state = {"cluster": cluster or base_cluster()}

    @app.on_event("startup")
    async def clock() -> None:
        async def tick():
            while True:
                await asyncio.sleep(seconds_per_minute)
                state["cluster"].advance(1)

        if seconds_per_minute > 0:
            app.state.clock = asyncio.create_task(tick())

    @app.get("/minute")
    def minute() -> float:
        return state["cluster"].minute

    @app.get("/deployments")
    def deployments() -> list[dict[str, Any]]:
        return [deployment_json(d) for d in state["cluster"].deployments()]

    @app.get("/deployments/{namespace}/{name}")
    def deployment(namespace: str, name: str) -> dict[str, Any]:
        try:
            return deployment_json(state["cluster"].deployment(name, namespace))
        except KubernetesAdapterError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/deployments/{namespace}/{name}/patch")
    def patch(namespace: str, name: str, body: PatchBody) -> dict[str, int]:
        ops = [JsonPatchOperation.model_validate(op) for op in body.patch]
        try:
            return {"generation": state["cluster"].apply_patch(namespace, name, ops)}
        except KubernetesAdapterError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/signals/{service_id}")
    def signals(service_id: str, namespace: str | None = None) -> dict[str, float]:
        return state["cluster"].signals(service_id, namespace)

    scenarios = {
        "memory_leak_after_deploy": lambda c: memory_leak_after_deploy(c, minutes=9),
        "cpu_load_spike": cpu_load_spike,
        "reset": None,
    }

    @app.post("/scenarios/{name}")
    def scenario(name: str) -> dict[str, Any]:
        if name not in scenarios:
            raise HTTPException(404, "unknown scenario")
        if name == "reset":
            state["cluster"] = base_cluster()
        else:
            scenarios[name](state["cluster"])
        return {"scenario": name, "minute": state["cluster"].minute}

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app


class RemoteCluster(SimulatedCluster):
    """`SimulatedCluster` API over HTTP (thread-safe; used from worker threads too)."""

    def __init__(self, base_url: str = "", timeout: float = 5.0, *, client: httpx.Client | None = None) -> None:
        # No super().__init__(): the state lives on the cluster service.
        self._lock = threading.RLock()
        self._http = client or httpx.Client(base_url=base_url, timeout=timeout)
        self.mutations: list[dict] = []

    @property
    def minute(self) -> float:  # type: ignore[override]
        return float(self._http.get("/minute").raise_for_status().json())

    @minute.setter
    def minute(self, value: float) -> None:
        raise AttributeError("simulated time is owned by the cluster service")

    def _get(self, namespace: str, name: str) -> SimDeployment:
        response = self._http.get(f"/deployments/{namespace}/{name}")
        if response.status_code == 404:
            raise KubernetesAdapterError(f"deployment {namespace}/{name} not found")
        return deployment_from_json(response.raise_for_status().json())

    def deployments(self) -> list[SimDeployment]:
        return [deployment_from_json(d) for d in self._http.get("/deployments").raise_for_status().json()]

    def apply_patch(self, namespace: str, name: str, patch) -> int:
        body = {"patch": [op.model_dump(mode="json") if hasattr(op, "model_dump") else op for op in patch]}
        response = self._http.post(f"/deployments/{namespace}/{name}/patch", json=body)
        if response.status_code == 409:
            raise KubernetesAdapterError(response.json()["detail"])
        generation = int(response.raise_for_status().json()["generation"])
        self.mutations.append({"service": name, "namespace": namespace, "generation": generation})
        return generation

    def signals(self, service_id: str, namespace: str | None = None) -> dict[str, float]:
        params = {"namespace": namespace} if namespace else None
        return self._http.get(f"/signals/{service_id}", params=params).raise_for_status().json()

    def scenario(self, name: str) -> dict[str, Any]:
        return self._http.post(f"/scenarios/{name}").raise_for_status().json()
