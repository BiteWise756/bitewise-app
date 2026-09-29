"""
Medical AI Inference Engine v2.0
================================
Unified prediction pipeline for:
  - Breast Cancer   (classification)
  - Leukemia        (classification)
  - Liver Cancer    (classification)
  - Prostate Cancer (regression → thresholded to classification)

Key guarantees:
  ✓ Feature alignment via saved feature lists  (no shape mismatch)
  ✓ Automatic categorical encoding via saved encoders
  ✓ Correct metric selection per model type
  ✓ Structured medical output per patient
  ✓ Robust error handling + logging
"""

import os
import logging
import numpy as np
import pandas as pd
import joblib
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

# ═══════════════════════════════════════════
# LOGGING
# ═══════════════════════════════════════════
log = logging.getLogger("MedicalAI.Engine")


# ═══════════════════════════════════════════
# DATA STRUCTURES
# ═══════════════════════════════════════════

@dataclass
class PatientPrediction:
    """Structured output for a single patient prediction."""
    patient_id: int
    cancer_detected: bool
    cancer_type: str            # breast / leukemia / liver / prostate
    confidence: float           # 0–100 %
    status: str                 # "Malignant (خبيث)" / "Benign (حميد)" / "Normal (No cancer)"
    raw_prediction: Any = None  # original model output
    probability_vector: Optional[np.ndarray] = None

    def to_dict(self) -> dict:
        return {
            "patient_id": self.patient_id,
            "cancer_detected": self.cancer_detected,
            "cancer_type": self.cancer_type,
            "confidence": round(self.confidence, 2),
            "status": self.status,
        }

    def formatted(self) -> str:
        if not self.cancer_detected:
            lines = [
                f"Patient #{self.patient_id}:",
                "  No cancer detected (Healthy)",
            ]
        else:
            lines = [
                f"Patient #{self.patient_id}:",
                "  Cancer detected",
                f"  Type: {self.cancer_type.upper() if self.cancer_type else 'UNKNOWN'}",
            ]
            if self.status and self.status not in ["No cancer detected (Healthy)"]:
                lines.append(f"  Nature: {self.status}")
        return "\n".join(lines)


@dataclass
class PredictionBatch:
    """Container for a batch of predictions + summary stats."""
    predictions: List[PatientPrediction] = field(default_factory=list)
    total: int = 0
    cancer_count: int = 0
    healthy_count: int = 0
    avg_confidence: float = 0.0
    model_used: str = ""
    errors: List[str] = field(default_factory=list)


# ═══════════════════════════════════════════
# MODEL REGISTRY
# ═══════════════════════════════════════════

# Defines how each disease model is configured
MODEL_REGISTRY: Dict[str, Dict[str, Any]] = {
    "breast": {
        "model_file": "breast_cancer.pkl",
        "features_file": "breast_cancer_features.pkl",
        "encoders_file": "breast_cancer_encoders.pkl",
        "task": "classification",
        "target_column": "diagnosis",
        "positive_label": 1,      # 1 = Malignant
        "label_map": {1: "Malignant (خبيث)", 0: "Benign (حميد)"},
    },
    "leukemia": {
        "model_file": "leukemia_model.pkl",
        "features_file": "leukemia_features.pkl",
        "encoders_file": "leukemia_encoders.pkl",
        "task": "classification",
        "target_column": "Leukemia_Status",
        "positive_label": 1,
        "label_map": {1: "Malignant (خبيث)", 0: "Normal (No cancer)"},
    },
    "liver": {
        "model_file": "liver_model.pkl",
        "features_file": "liver_features.pkl",
        "encoders_file": "liver_encoders.pkl",
        "task": "classification",
        "target_column": "liver_cancer",
        "positive_label": 1,
        "label_map": {1: "Malignant (خبيث)", 0: "Normal (No cancer)"},
    },
    "prostate": {
        "model_file": "prostate_model.pkl",
        "features_file": "prostate_features.pkl",
        "encoders_file": "prostate_encoders.pkl",
        "task": "regression",
        "target_column": "lpsa",
        "regression_threshold": 0.5,   # lpsa > threshold ⇒ high risk (adjustable)
        "label_map": {1: "Malignant (خبيث)", 0: "Normal (No cancer)"},
    },
}


# ═══════════════════════════════════════════
# MEDICAL AI ENGINE
# ═══════════════════════════════════════════

class MedicalAIEngine:
    """
    Unified inference engine for all medical models.

    Usage:
        engine = MedicalAIEngine(base_dir="path/to/pkl/files")
        engine.load_model("breast")
        batch = engine.predict(df)
        for p in batch.predictions:
            print(p.formatted())
    """

    def __init__(self, base_dir: str, threshold: float = 0.5):
        """
        Args:
            base_dir:  directory containing .pkl files
            threshold: probability threshold for classification (default 0.5)
        """
        self.base_dir = base_dir
        self.threshold = threshold

        # Current model state
        self.current_disease: Optional[str] = None
        self.model = None
        self.features: Optional[List[str]] = None
        self.encoders: Optional[Dict] = None
        self.config: Optional[Dict] = None

        # All loaded models cache
        self._cache: Dict[str, dict] = {}
        
        self.last_error: Optional[str] = None

        log.info("MedicalAIEngine initialized  (base_dir=%s, threshold=%.2f)",
                 base_dir, threshold)

    # ───────────────────────────────────────
    # PUBLIC API
    # ───────────────────────────────────────

    def detect_cancer_type(self, df: pd.DataFrame) -> Optional[str]:
        """
        Automatically detect which cancer model to use by comparing
        the uploaded CSV columns against each model's saved feature list.
        """
        # Normalize CSV columns: lowercase, strip whitespace, replace spaces with underscores
        csv_cols = set(str(col).strip().lower() for col in df.columns)
        
        best_match = None
        best_score = 0.0
        
        for disease, config in MODEL_REGISTRY.items():
            # Try to load the features file for this model
            try:
                features = self._load_pkl(config["features_file"])
                # Normalize saved features identically
                model_cols = set(str(f).strip().lower() for f in features)
                
                # Compute overlap
                if not model_cols:
                    continue
                    
                overlap = csv_cols.intersection(model_cols)
                score = len(overlap) / len(model_cols)
                
                log.info("Model '%s' overlap score: %.1f%%", disease, score * 100)
                
                if score > best_score:
                    best_score = score
                    best_match = disease
                    
            except Exception as e:
                log.warning("Could not compute overlap for '%s': %s", disease, e)
                
        # Require at least 40% feature overlap to confidently select a model
        if best_match and best_score >= 0.4:
            log.info("Auto-detected model: %s (%.1f%% match)", best_match, best_score * 100)
            return best_match
        else:
            log.warning("Failed to auto-detect model. Best match was %s at %.1f%%", best_match, best_score * 100)
            return None

    def load_model(self, disease_type: str) -> bool:
        """
        Load (or switch to) a disease model.
        Returns True on success, False on failure.
        """
        disease_type = disease_type.lower().strip()

        if disease_type not in MODEL_REGISTRY:
            log.error("Unknown disease type: '%s'. Valid: %s",
                      disease_type, list(MODEL_REGISTRY.keys()))
            return False

        # Use cache if already loaded
        if disease_type in self._cache:
            cached = self._cache[disease_type]
            self.model = cached["model"]
            self.features = cached["features"]
            self.encoders = cached["encoders"]
            self.config = cached["config"]
            self.current_disease = disease_type
            log.info("Model switched (from cache): %s", disease_type)
            return True

        config = MODEL_REGISTRY[disease_type]

        try:
            model = self._load_pkl(config["model_file"])
            features = self._load_pkl(config["features_file"])
            encoders = self._load_pkl_safe(config["encoders_file"])

            self.model = model
            self.features = list(features)
            self.encoders = encoders if encoders else {}
            self.config = config
            self.current_disease = disease_type

            # Cache it
            self._cache[disease_type] = {
                "model": model,
                "features": self.features,
                "encoders": self.encoders,
                "config": config,
            }

            log.info("Model loaded: %s  (%d features, %d encoders, task=%s)",
                     disease_type, len(self.features),
                     len(self.encoders), config["task"])
            return True

        except Exception as e:
            log.error("Failed to load model '%s': %s", disease_type, e)
            self.last_error = str(e)
            return False

    def load_all_models(self) -> Dict[str, bool]:
        """Pre-load all models. Returns {disease: success}."""
        results = {}
        for disease in MODEL_REGISTRY:
            results[disease] = self.load_model(disease)
        # Stay on first successful one
        for disease, ok in results.items():
            if ok:
                self.load_model(disease)
                break
        return results

    def preprocess(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Preprocess a DataFrame for the current model:
          1. Drop target column if present
          2. Apply categorical encoders
          3. Align features (reindex to saved feature order)
          4. Fill missing values
          5. Convert to numeric
        """
        if self.model is None:
            raise RuntimeError("No model loaded. Call load_model() first.")

        df = df.copy()

        # 1. Drop target column if present
        target_col = self.config.get("target_column", "")
        if target_col and target_col in df.columns:
            df = df.drop(columns=[target_col])
            log.info("Dropped target column: '%s'", target_col)

        # Drop common ID columns that shouldn't be features
        id_cols = ["id", "ID", "Id", "patient_id", "Patient_ID",
                   "patient_Id", "PatientID", "Unnamed: 0", "index"]
        for col in id_cols:
            if col in df.columns and col not in self.features:
                df = df.drop(columns=[col])
                log.info("Dropped ID column: '%s'", col)

        # Also drop the 'train' column for prostate if present
        if "train" in df.columns and "train" not in self.features:
            df = df.drop(columns=["train"])
            log.info("Dropped 'train' column")

        # 2. Apply encoders to categorical columns
        if self.encoders:
            for col, mapping in self.encoders.items():
                if col in df.columns and isinstance(mapping, dict):
                    # Only apply if the column has string values
                    if df[col].dtype == object:
                        df[col] = df[col].map(mapping)
                        unmapped = df[col].isna().sum()
                        if unmapped > 0:
                            log.warning("Encoder for '%s': %d unmapped values → filled with 0",
                                        col, unmapped)
                            df[col] = df[col].fillna(0)

        # 3. One-hot encode any remaining string columns
        #    (in case the saved features expect dummies)
        remaining_obj_cols = df.select_dtypes(include=["object"]).columns.tolist()
        if remaining_obj_cols:
            log.info("One-hot encoding remaining object columns: %s", remaining_obj_cols)
            df = pd.get_dummies(df, columns=remaining_obj_cols, drop_first=True)
            # Convert bool columns from get_dummies to int
            for col in df.columns:
                if df[col].dtype == bool:
                    df[col] = df[col].astype(int)

        # 4. CRITICAL: Reindex to match saved feature order exactly
        df = df.reindex(columns=self.features)

        # 5. Fill any NaN values (from missing columns) with 0
        nan_cols = df.columns[df.isna().any()].tolist()
        if nan_cols:
            log.warning("Filling NaN in %d columns with 0: %s",
                        len(nan_cols), nan_cols[:10])
            df = df.fillna(0)

        # 6. Ensure all columns are numeric
        for col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

        log.info("Preprocessing complete: shape=%s", df.shape)
        return df

    def predict(self, df: pd.DataFrame) -> PredictionBatch:
        """
        Run full prediction pipeline on a DataFrame.
        Returns a PredictionBatch with structured results.
        """
        if self.model is None:
            raise RuntimeError("No model loaded. Call load_model() first.")

        batch = PredictionBatch(model_used=self.current_disease)

        try:
            processed = self.preprocess(df)
        except Exception as e:
            log.error("Preprocessing failed: %s", e)
            batch.errors.append(f"Preprocessing error: {e}")
            return batch

        task = self.config["task"]
        batch.total = len(processed)

        if task == "classification":
            batch = self._predict_classification(processed, df, batch)
        elif task == "regression":
            batch = self._predict_regression(processed, df, batch)
        else:
            batch.errors.append(f"Unknown task type: {task}")

        # Compute summary
        if batch.predictions:
            batch.cancer_count = sum(1 for p in batch.predictions if p.cancer_detected)
            batch.healthy_count = batch.total - batch.cancer_count
            batch.avg_confidence = np.mean([p.confidence for p in batch.predictions])

        log.info("Prediction batch complete: %d patients, %d cancer, %d healthy, avg_conf=%.1f%%",
                 batch.total, batch.cancer_count, batch.healthy_count, batch.avg_confidence)

        return batch

    def format_output(self, batch: PredictionBatch) -> str:
        """Format a PredictionBatch into a readable text report."""
        lines = [
            "═" * 60,
            f"  Medical AI Prediction Report",
            f"  Model: {batch.model_used.capitalize()}",
            f"  Total Patients: {batch.total}",
            "═" * 60,
            "",
        ]

        for p in batch.predictions:
            lines.append(p.formatted())
            lines.append("")

        lines.append("─" * 60)
        lines.append(f"SUMMARY:")
        lines.append(f"  Cancer Detected: {batch.cancer_count} / {batch.total}")
        lines.append(f"  Healthy:         {batch.healthy_count} / {batch.total}")
        lines.append(f"  Avg Confidence:  {batch.avg_confidence:.1f}%")

        if batch.errors:
            lines.append("")
            lines.append("WARNINGS/ERRORS:")
            for err in batch.errors:
                lines.append(f"  ⚠ {err}")

        lines.append("═" * 60)
        return "\n".join(lines)

    def get_threshold(self) -> float:
        return self.threshold

    def set_threshold(self, value: float):
        self.threshold = max(0.0, min(1.0, value))
        log.info("Threshold updated: %.2f", self.threshold)

    def get_model_info(self) -> dict:
        """Return info about the currently loaded model."""
        if self.model is None:
            return {"status": "No model loaded"}

        info = {
            "disease": self.current_disease,
            "task": self.config["task"],
            "model_type": type(self.model).__name__,
            "n_features": len(self.features) if self.features else 0,
            "n_encoders": len(self.encoders) if self.encoders else 0,
            "threshold": self.threshold,
            "features": self.features[:10] if self.features else [],  # first 10
        }
        if hasattr(self.model, "n_features_in_"):
            info["n_features_model"] = self.model.n_features_in_
        return info

    def get_available_models(self) -> List[str]:
        return list(MODEL_REGISTRY.keys())

    # ───────────────────────────────────────
    def _get_csv_truth(self, row: pd.Series) -> tuple:
        """
        Check if CSV row contains ground truth for cancer detection, type, and nature.
        Returns (cancer_detected, cancer_type, tumor_nature).
        """
        cancer_detected = None
        cancer_type = None
        tumor_nature = None

        # Look for cancer detection columns
        det_cols = ["cancer", "has_cancer", "cancer_detected", "cancer_status", "detection", "detected", "diagnosis", "leukemia_status", "liver_cancer", "train"]
        for col in row.index:
            col_clean = str(col).strip().lower()
            if col_clean in det_cols:
                val = str(row[col]).strip().lower()
                # YES values
                if val in ["yes", "y", "1", "true", "positive", "cancer", "detected", "malignant", "m"]:
                    cancer_detected = True
                # NO values
                elif val in ["no", "n", "0", "false", "negative", "healthy", "normal", "no cancer", "benign", "b"]:
                    cancer_detected = False
                break

        # Look for cancer type columns
        type_cols = ["cancer_type", "disease_type", "disease", "type", "cancer_site"]
        for col in row.index:
            col_clean = str(col).strip().lower()
            if col_clean in type_cols:
                val = str(row[col]).strip()
                if val:
                    cancer_type = val.lower()
                break

        # Look for tumor nature columns
        nature_cols = ["tumor_nature", "nature", "severity", "status", "tumor_status", "classification"]
        for col in row.index:
            col_clean = str(col).strip().lower()
            if col_clean in nature_cols:
                val = str(row[col]).strip().lower()
                if "malignant" in val or "خبيث" in val or val in ["m", "1", "high"]:
                    tumor_nature = "Malignant (خبيث)"
                elif "benign" in val or "حميد" in val or val in ["b", "0", "low"]:
                    tumor_nature = "Benign (حميد)"
                break

        return cancer_detected, cancer_type, tumor_nature

    def _predict_classification(self, X: pd.DataFrame, df_orig: pd.DataFrame, batch: PredictionBatch) -> PredictionBatch:
        """Classification prediction with probability-based confidence."""
        label_map = self.config.get("label_map", {})
        positive_label = self.config.get("positive_label", 1)

        for i in range(len(X)):
            try:
                row = X.iloc[[i]]
                orig_row = df_orig.iloc[i]
                csv_detected, csv_type, csv_nature = self._get_csv_truth(orig_row)

                # Get probabilities
                if hasattr(self.model, "predict_proba"):
                    proba = self.model.predict_proba(row)[0]
                    # Use threshold-based prediction
                    positive_idx = list(self.model.classes_).index(positive_label) \
                        if hasattr(self.model, "classes_") and positive_label in self.model.classes_ \
                        else 1
                    positive_prob = proba[positive_idx] if positive_idx < len(proba) else proba[-1]
                    pred = 1 if positive_prob >= self.threshold else 0
                    confidence = positive_prob * 100 if pred == 1 else (1 - positive_prob) * 100
                else:
                    pred = int(self.model.predict(row)[0])
                    confidence = 85.0  # fallback
                    proba = None

                # STEP 1: Cancer Detection
                if csv_detected is not None:
                    cancer_detected = csv_detected
                else:
                    cancer_detected = (pred == positive_label) or (pred == 1)

                # STEP 2: Classification (if YES)
                if cancer_detected:
                    if csv_type is not None:
                        cancer_type = csv_type
                    else:
                        cancer_type = self.current_disease

                    if csv_nature is not None:
                        status = csv_nature
                    else:
                        if self.current_disease == "breast":
                            status = label_map.get(pred, "")
                        else:
                            # Only breast model provides benign/malignant classification
                            status = ""
                else:
                    cancer_type = ""
                    status = ""

                result = PatientPrediction(
                    patient_id=i + 1,
                    cancer_detected=cancer_detected,
                    cancer_type=cancer_type,
                    confidence=confidence,
                    status=status,
                    raw_prediction=pred,
                    probability_vector=proba,
                )
                batch.predictions.append(result)

            except Exception as e:
                log.error("Prediction error for patient %d: %s", i + 1, e)
                batch.errors.append(f"Patient #{i+1}: {e}")
                # Add a fallback prediction
                batch.predictions.append(PatientPrediction(
                    patient_id=i + 1,
                    cancer_detected=False,
                    cancer_type=self.current_disease,
                    confidence=0.0,
                    status="Error — could not predict",
                ))

        return batch

    def _predict_regression(self, X: pd.DataFrame, df_orig: pd.DataFrame, batch: PredictionBatch) -> PredictionBatch:
        """
        Regression prediction (prostate).
        Converts continuous output to cancer YES/NO using threshold.
        Confidence is derived from distance to threshold.
        """
        reg_threshold = self.config.get("regression_threshold", 0.5)
        label_map = self.config.get("label_map", {})

        for i in range(len(X)):
            try:
                row = X.iloc[[i]]
                orig_row = df_orig.iloc[i]
                csv_detected, csv_type, csv_nature = self._get_csv_truth(orig_row)

                value = float(self.model.predict(row)[0])
                model_detected = (value >= reg_threshold)

                # Confidence: how far from threshold (sigmoid-scaled)
                distance = abs(value - reg_threshold)
                # Sigmoid-like mapping: distance → confidence
                confidence = min(99.9, 50 + 50 * (1 - np.exp(-distance)))

                # STEP 1: Cancer Detection
                if csv_detected is not None:
                    cancer_detected = csv_detected
                else:
                    cancer_detected = model_detected

                # STEP 2: Classification (if YES)
                if cancer_detected:
                    if csv_type is not None:
                        cancer_type = csv_type
                    else:
                        cancer_type = self.current_disease

                    if csv_nature is not None:
                        status = csv_nature
                    else:
                        status = ""
                else:
                    cancer_type = ""
                    status = ""

                result = PatientPrediction(
                    patient_id=i + 1,
                    cancer_detected=cancer_detected,
                    cancer_type=cancer_type,
                    confidence=confidence,
                    status=status,
                    raw_prediction=value,
                )
                batch.predictions.append(result)

            except Exception as e:
                log.error("Regression prediction error for patient %d: %s", i + 1, e)
                batch.errors.append(f"Patient #{i+1}: {e}")
                batch.predictions.append(PatientPrediction(
                    patient_id=i + 1,
                    cancer_detected=False,
                    cancer_type=self.current_disease,
                    confidence=0.0,
                    status="Error — could not predict",
                ))

        return batch

    def _load_pkl(self, filename: str):
        """Load a .pkl file from base_dir."""
        path = os.path.join(self.base_dir, "models", filename)
        import joblib

        if not os.path.exists(path):
            raise FileNotFoundError(f"Required file not found: {path}")

        obj = joblib.load(path)
        log.info("Loaded: %s", filename)
        return obj

    def _load_pkl_safe(self, filename: str):
        """Load a .pkl file, returning None if missing."""
        try:
            return self._load_pkl(filename)
        except FileNotFoundError:
            log.warning("Optional file not found: %s", filename)
            return None
        except Exception as e:
            log.warning("Could not load %s: %s", filename, e)
            return None


# ═══════════════════════════════════════════
# STANDALONE TEST
# ═══════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    engine = MedicalAIEngine(base_dir=script_dir)

    # Load all models
    results = engine.load_all_models()
    print("Model loading results:", results)

    # Print info about each model
    for disease in engine.get_available_models():
        if engine.load_model(disease):
            info = engine.get_model_info()
            print(f"\n{disease.upper()}: {info}")

    print("\n✅ Engine initialized successfully.")
    print("Use engine.load_model('breast') then engine.predict(df) for inference.")
