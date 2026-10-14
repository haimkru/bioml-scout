"""Gloss acronyms in evaluation text, for papers evaluated before the plain-language rule.

The evaluator is now told to explain acronyms itself. This is a safety net for text already on
disk, and for anything it misses: a fixed table of terms that keep appearing, applied in code so
no agent calls are needed. Only the first use in each field is glossed, and only when the text
doesn't already explain it.

Add a term when you hit one you had to look up. Keep the gloss under ~6 words.
"""
from __future__ import annotations

import re

# Deliberately not listed: terms whose meaning the surrounding sentence always carries anyway
# (DNA, RNA, PDF, CSV, HTML, URL, API), terms a general reader already knows (CPU, RAM), and
# one-off model names the text introduces itself.
GLOSSARY: dict[str, str] = {
    # compute
    "GPU": "graphics card, which this laptop lacks",
    # evaluation
    "CV": "cross-validation",
    "LODO": "leave-one-donor-out",
    "LOSO": "leave-one-study-out",
    "AUROC": "area under the ROC curve, 0.5 is chance",
    "AUC": "area under the curve",
    "AUPRC": "precision-recall area under the curve",
    "PR-AUC": "precision-recall area under the curve",
    "ROC": "receiver operating characteristic",
    "MAE": "mean absolute error",
    "RMSE": "root mean squared error",
    "R2": "fraction of variance explained",
    "QC": "quality control",
    "PCA": "principal component analysis",
    "DE": "differential expression",
    "FDR": "false discovery rate",
    "MCC": "Matthews correlation coefficient",
    # methods and models
    "ML": "machine learning",
    "LLM": "large language model",
    "GBM": "gradient-boosted trees",
    "MLP": "a small neural network",
    "CNN": "convolutional neural network",
    "LoRA": "low-rank fine-tuning",
    "DMS": "deep mutational scanning",
    "MPRA": "massively parallel reporter assay",
    "GWAS": "genome-wide association study",
    "QTL": "genetic variants affecting a trait",
    "eQTL": "genetic variants affecting gene expression",
    "scRNA-seq": "single-cell RNA sequencing",
    "snRNA-seq": "single-nucleus RNA sequencing",
    "ATAC-seq": "an assay for open chromatin",
    "ONT": "Oxford Nanopore long-read sequencing",
    "PacBio": "a long-read sequencing platform",
    "SMILES": "a text format for molecule structures",
    "UMI": "unique molecular identifier",
    "DAR": "differentially accessible region",
    # repositories and access
    "GEO": "NCBI's Gene Expression Omnibus repository",
    "SRA": "NCBI's Sequence Read Archive",
    "PMC": "PubMed Central",
    "EPMC": "Europe PubMed Central",
    "dbGaP": "a controlled-access human genetics archive",
    "EGA": "the European Genome-phenome Archive, controlled access",
    "NeMO": "a neuroscience data archive",
    "MaveDB": "a database of mutational scans",
    "UKB": "UK Biobank",
    "CC0": "public domain licence",
    "CC-BY": "attribution required, otherwise free",
    "CC-BY-NC": "no commercial use allowed",
    "GPL-3.0": "copyleft open-source",
    "MIT": "permissive open-source",
}
# Longest first, so "PR-AUC" wins over "AUC" and "snRNA-seq" over "RNA-seq".
_TERMS = sorted(GLOSSARY, key=len, reverse=True)
_PATTERN = re.compile(r"(?<![\w-])(" + "|".join(re.escape(t) for t in _TERMS) + r")(?![\w-])")


def gloss(text: str | None, used: set[str] | None = None) -> str:
    """Add "(what it means)" after the first use of each known acronym.

    Pass a shared `used` set to gloss a term once across several fields read together
    (a report); leave it out when each field is read on its own (a tooltip).
    """
    if not text:
        return ""
    used = used if used is not None else set()

    def sub(m: re.Match[str]) -> str:
        term = m.group(1)
        rest = text[m.end():m.end() + 80]
        explained_here = bool(re.match(r"\s*[\(\[]", rest))
        if term in used or explained_here:
            used.add(term)  # explained once is enough, whoever did it
            return term
        # "CC0 licence" shouldn't become "CC0 (public domain licence) licence".
        gloss_text = GLOSSARY[term]
        tail = gloss_text.split()[-1].rstrip(",")
        if re.match(rf"\s+{re.escape(tail)}\b", rest, re.I):
            gloss_text = " ".join(gloss_text.split()[:-1])
            if not gloss_text:
                return term
        used.add(term)
        return f"{term} ({gloss_text})"

    return _PATTERN.sub(sub, text)
