from pydantic import BaseModel


class BasePublicID(BaseModel):
    public_id: str


class BaseModelSchema(BasePublicID):
    name: str
