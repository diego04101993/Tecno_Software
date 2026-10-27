from datetime import datetime

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator
from pydantic_core import PydanticCustomError


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class TimestampedModel(ORMModel):
    id: str
    created_at: datetime
    updated_at: datetime | None = None


class MessageResponse(BaseModel):
    message: str


class SensitiveRequest(BaseModel):
    """Remove credential-bearing inputs from ALL validation errors."""

    @field_validator("*", mode="before")
    @classmethod
    def require_serializable_text(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("El texto debe contener Unicode valido.") from None
        return value

    @model_validator(mode="wrap")
    @classmethod
    def redact_validation_input(cls, value, handler):
        try:
            return handler(value)
        except ValidationError as exc:
            errors = []
            for error in exc.errors(include_url=False):
                # Missing-field errors can contain the entire request payload.
                error["input"] = None
                # EmailStr uses a custom value_error context, incompatible with
                # reconstruction as the built-in value_error. Preserve the useful
                # message/type explicitly; discard exception/context objects.
                message = error["msg"].encode("utf-8", errors="backslashreplace").decode("utf-8")
                errors.append({
                    "type": PydanticCustomError(error["type"], message),
                    "loc": error["loc"],
                    "input": error["input"],
                })
            raise ValidationError.from_exception_data(cls.__name__, errors) from None
