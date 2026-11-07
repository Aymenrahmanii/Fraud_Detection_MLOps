# Disposable Kubernetes harness

This harness is disabled unless `AGENT4_K8S_INTEGRATION` equals
`I_UNDERSTAND_DISPOSABLE_CLUSTER`. It accepts only an absolute kubeconfig, an
explicit context and cluster identity, a namespace whose name starts with
`agent4-it-`, and the fixed Deployment name `agent4-integration-target`.

`rbac.yaml` creates a dedicated namespace, ServiceAccount, namespaced Role and
RoleBinding. The Role can only `get` and `patch` that named Deployment. The
patch permission exists solely for API admission dry-run; tests always send
`dryRun=All`. There is no ClusterRole, Secret access, pod exec, arbitrary patch
interface, or live apply path.

An operator must create the disposable Deployment and arrange authentication
for the ServiceAccount. Then set every `AGENT4_K8S_*` variable described in
`harness.py` and run `pytest agent4_deployment_recovery/tests/integration`.
Never point this harness at production. Cleanup is operator-controlled:

```powershell
kubectl --context <explicit-test-context> delete namespace agent4-it-disposable
```

Deleting the namespace removes the test ServiceAccount, Role, RoleBinding and
Deployment. Agent 4 never performs that cleanup automatically.
