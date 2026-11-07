"""Canonical v1 contracts shared by the platform and the agent workers.

Same shapes as `agent1_monitoring/shared_contracts` (identical to Agent 2's
copy), plus `AgentResult.payload` (as in Agent 6) and the platform topics in
`topics.py`. Agents keep their own copies; workers translate at the boundary.
"""
