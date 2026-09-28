from pydantic import BaseModel, Field
from typing import Optional
import numpy as np

class FraudRequest(BaseModel):
    # Field types must match the model's actual input signature
    # (model.metadata.get_input_schema()), not just "look" numeric.
    # MLflow's schema enforcement only allows an implicit int64->float64
    # conversion when every value in the column is 0 or 1 (it treats
    # that as a boolean-like column); any other integer value is
    # correctly rejected as an unsafe conversion. paymentMethodAgeDays
    # and isWeekend are "double" in the model signature -- declaring
    # them as `int` here silently worked only for 0/1-valued requests
    # and threw a 500 for any other value (e.g. paymentMethodAgeDays=700).
    Category: Optional[str] = Field(None, example="shopping")
    paymentMethod: str = Field(..., example="paypal")
    isWeekend: Optional[float] = Field(None, example=None)
    numItems: int = Field(..., example=4)
    localTime: float = Field(..., example=4.742303)
    paymentMethodAgeDays: float = Field(..., example=0.0)
    accountAgeDays: int = Field(..., example=1)

class FraudResponse(BaseModel):
    prediction: int