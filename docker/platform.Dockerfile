# One image for every platform process (Context, HITL, Orchestrator, Dashboard,
# simulated cluster) and the six agent workers; the role is the command.
#
#   docker build -f docker/platform.Dockerfile -t aiops-platform:local .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MLFLOW_DISABLE_AGENT_HINT=1
WORKDIR /app

RUN pip install --no-cache-dir "uv>=0.8,<1"

COPY requirements.txt ./
COPY agent6_mlops_lifecycle/requirements.txt agent6_mlops_lifecycle/constraints-model.txt agent6_mlops_lifecycle/constraints-tested.txt ./agent6_mlops_lifecycle/
COPY agent1_monitoring/requirements.txt ./agent1_monitoring/
COPY agent2_diagnosis/requirements.txt ./agent2_diagnosis/
COPY agent3_security_quality/requirements.txt ./agent3_security_quality/
COPY agent5_resource_optimization/requirements.txt ./agent5_resource_optimization/
COPY aiops_platform/requirements.txt ./aiops_platform/
COPY docker/agent4-requirements.txt docker/overrides.txt ./docker/

# Agent 6's tested lock constrains everything, except two documented conflicts:
# drain3 (Agent 1) pins cachetools==4.2.1 while mlflow 3 needs >=5 (override),
# and Agent 4 pins an older cryptography (left to the resolver).
RUN grep -v -E '^(cachetools|cryptography|pywin32)==' agent6_mlops_lifecycle/constraints-tested.txt > /tmp/constraints.txt \
 && uv pip install --system --no-cache -c /tmp/constraints.txt --override docker/overrides.txt \
      -r requirements.txt \
      -r agent1_monitoring/requirements.txt \
      -r agent2_diagnosis/requirements.txt \
      -r agent3_security_quality/requirements.txt \
      -r docker/agent4-requirements.txt \
      -r agent5_resource_optimization/requirements.txt \
      -r aiops_platform/requirements.txt

COPY agent1_monitoring ./agent1_monitoring
COPY agent2_diagnosis ./agent2_diagnosis
COPY agent3_security_quality ./agent3_security_quality
COPY agent4_deployment_recovery ./agent4_deployment_recovery
COPY agent5_resource_optimization ./agent5_resource_optimization
COPY agent6_mlops_lifecycle ./agent6_mlops_lifecycle
COPY aiops_platform ./aiops_platform

RUN useradd --create-home --uid 1000 --shell /bin/false aiops \
 && mkdir -p /data /secrets /trust \
 && chown -R aiops:aiops /app /data /secrets /trust
USER aiops

ENTRYPOINT ["python", "-m", "aiops_platform.services"]
