from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class PaymentMethod(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PAYMENT_METHOD_UNSPECIFIED: _ClassVar[PaymentMethod]
    PAYMENT_METHOD_CARD: _ClassVar[PaymentMethod]
    PAYMENT_METHOD_WALLET: _ClassVar[PaymentMethod]
    PAYMENT_METHOD_COD: _ClassVar[PaymentMethod]
PAYMENT_METHOD_UNSPECIFIED: PaymentMethod
PAYMENT_METHOD_CARD: PaymentMethod
PAYMENT_METHOD_WALLET: PaymentMethod
PAYMENT_METHOD_COD: PaymentMethod

class CartLine(_message.Message):
    __slots__ = ("sku", "quantity")
    SKU_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    sku: str
    quantity: int
    def __init__(self, sku: _Optional[str] = ..., quantity: _Optional[int] = ...) -> None: ...

class PlaceOrderRequest(_message.Message):
    __slots__ = ("items", "customer", "payment_method", "idempotency_key")
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    CUSTOMER_FIELD_NUMBER: _ClassVar[int]
    PAYMENT_METHOD_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_KEY_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[CartLine]
    customer: str
    payment_method: PaymentMethod
    idempotency_key: str
    def __init__(self, items: _Optional[_Iterable[_Union[CartLine, _Mapping]]] = ..., customer: _Optional[str] = ..., payment_method: _Optional[_Union[PaymentMethod, str]] = ..., idempotency_key: _Optional[str] = ...) -> None: ...

class PlaceOrderResponse(_message.Message):
    __slots__ = ("order_id", "status", "total_cents", "item_count", "replayed", "request_id")
    ORDER_ID_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    TOTAL_CENTS_FIELD_NUMBER: _ClassVar[int]
    ITEM_COUNT_FIELD_NUMBER: _ClassVar[int]
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    order_id: int
    status: str
    total_cents: int
    item_count: int
    replayed: bool
    request_id: str
    def __init__(self, order_id: _Optional[int] = ..., status: _Optional[str] = ..., total_cents: _Optional[int] = ..., item_count: _Optional[int] = ..., replayed: _Optional[bool] = ..., request_id: _Optional[str] = ...) -> None: ...
