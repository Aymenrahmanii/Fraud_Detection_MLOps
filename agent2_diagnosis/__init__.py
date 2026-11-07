"""Agent 2: evidence-grounded RCA; analysis only."""

from agent2_diagnosis.contracts import (
    DiagnosisAgentResult,
    DiagnosisInput,
    DiagnosisRecommendation,
    DiagnosisResult,
    EvidenceItem,
    Hypothesis,
    KnowledgeDocument,
    KnowledgeRetrieval,
    SupplementalAnalysis,
)
from agent2_diagnosis.evidence_collector import collect_evidence
from agent2_diagnosis.llm_analyzer import (
    ClaudeSupplementalAnalyzer,
    DeterministicSupplementalAnalyzer,
    analyze_with_llm,
)
from agent2_diagnosis.qdrant_retriever import QdrantKnowledgeRetriever
from agent2_diagnosis.rag_retriever import (
    LocalKnowledgeRetriever,
    retrieve_runbooks,
    retrieve_similar_incidents,
)
from agent2_diagnosis.result_builder import build_result, to_diagnosis_completed
from agent2_diagnosis.rules import evaluate_hypotheses
from agent2_diagnosis.service import DiagnosisAgent
from agent2_diagnosis.tools import Toolbox, build_toolbox_from_env, static_toolbox

__all__ = [
    "DiagnosisAgent",
    "DiagnosisAgentResult",
    "DiagnosisInput",
    "EvidenceItem",
    "Hypothesis",
    "DiagnosisRecommendation",
    "DiagnosisResult",
    "KnowledgeDocument",
    "KnowledgeRetrieval",
    "SupplementalAnalysis",
    "collect_evidence",
    "evaluate_hypotheses",
    "retrieve_runbooks",
    "retrieve_similar_incidents",
    "LocalKnowledgeRetriever",
    "analyze_with_llm",
    "DeterministicSupplementalAnalyzer",
    "ClaudeSupplementalAnalyzer",
    "QdrantKnowledgeRetriever",
    "Toolbox",
    "build_toolbox_from_env",
    "static_toolbox",
    "build_result",
    "to_diagnosis_completed",
]
