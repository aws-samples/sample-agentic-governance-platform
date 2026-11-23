"""The marketplace's rows, on ``core.state_store.StateStore``.

One partition, ``pk="MARKETPLACE"``, holds three record kinds told apart by ``_record_kind`` beside ``sk``:
a subscription keyed by its id, a listing by ``listing#<type>#<id>`` and a publish request by
``publish#<type>#<id>``, one per product. A subscription is created on the condition that its id is
new, and a subscription or a publish request is saved whole on the condition that its row's ``status`` is
one the caller read; a listing writes only the fields it sets. A read fault is logged and answers absent;
a write fault propagates.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Collection
from typing import Any, Dict, List, Optional

from core.state_store import STORE_FAULTS, StateStore
from models.marketplace import Listing, ProductType, PublishRequest, Subscription, SubscriptionStatus
from services.marketplace_errors import MarketplaceError

logger = logging.getLogger(__name__)

_PARTITION_KEY = "MARKETPLACE"
_KIND_SUBSCRIPTION = "subscription"
_KIND_LISTING = "listing"
_KIND_PUBLISH = "publish"

_ACTIVE_STATUSES = (SubscriptionStatus.PENDING, SubscriptionStatus.APPROVED)

_PUBLISH_MOVED = "Another write moved the publish request first"
_SUBSCRIPTION_MOVED = "Another write moved the subscription first"


def _listing_sk(product_type: str, product_id: str) -> str:
    return f"listing#{product_type}#{product_id}"


def _publish_sk(product_type, product_id: str) -> str:
    """One publish record per product, keyed by the ``(product_type, product_id)`` pair so the two
    registries' id spaces never collide; the type is normalized through ``ProductType`` because a
    ``(str, Enum)`` member formats as its name, not its value."""
    return f"publish#{ProductType(product_type).value}#{product_id}"


class MarketplaceStore:
    def __init__(self, state_store: StateStore) -> None:
        self._store = state_store

    def active_sub_for(
        self,
        ptype: ProductType,
        product_id: str,
        requester_oid: str,
        agent_id: Optional[str],
    ) -> Optional[Subscription]:
        """The caller's pending or approved subscription for the key: ``(oid, product_id)`` for an
        agent product, ``(oid, agent_id, product_id)`` for an MCP product."""
        for s in self.load_subs():
            if s.status not in _ACTIVE_STATUSES:
                continue
            if s.product_type != ptype or s.product_id != product_id or s.requester_oid != requester_oid:
                continue
            if ptype is ProductType.MCP and s.agent_id != agent_id:
                continue
            return s
        return None

    def latest_sub_by_product(self, caller_oid: str, ptype: ProductType) -> Dict[str, Subscription]:
        latest: Dict[str, Subscription] = {}
        for s in self.load_subs():
            if s.requester_oid != caller_oid or s.product_type != ptype:
                continue
            cur = latest.get(s.product_id)
            if cur is None or s.created_at > cur.created_at:
                latest[s.product_id] = s
        return latest

    def get_sub(self, subscription_id: str) -> Optional[Subscription]:
        try:
            item = self._store.get((_PARTITION_KEY, subscription_id))
        except STORE_FAULTS:
            logger.exception("Failed to fetch subscription %s from DDB", subscription_id)
            return None
        if not item or item.get("_record_kind") != _KIND_SUBSCRIPTION:
            return None
        return self._sub_from_item(item)

    def load_subs(self) -> List[Subscription]:
        try:
            items = self._scan_partition()
        except STORE_FAULTS:
            logger.exception("Failed to load subscriptions from DDB")
            return []
        return [self._sub_from_item(i) for i in items if i.get("_record_kind") == _KIND_SUBSCRIPTION]

    def load_listings(self) -> Dict[str, Listing]:
        try:
            items = self._scan_partition()
        except STORE_FAULTS:
            logger.exception("Failed to load listings from DDB")
            return {}
        out: Dict[str, Listing] = {}
        for i in items:
            if i.get("_record_kind") != _KIND_LISTING:
                continue
            listing = self._listing_from_item(i)
            out[_listing_sk(listing.product_type.value, listing.product_id)] = listing
        return out

    def load_publish_requests(self) -> Dict[str, PublishRequest]:
        try:
            items = self._scan_partition()
        except STORE_FAULTS:
            logger.exception("Failed to load publish requests from DDB")
            return {}
        out: Dict[str, PublishRequest] = {}
        for i in items:
            if i.get("_record_kind") != _KIND_PUBLISH:
                continue
            req = self._publish_from_item(i)
            out[_publish_sk(req.product_type, req.product_id)] = req
        return out

    def get_publish_for_product(self, product_type, product_id: str) -> Optional[PublishRequest]:
        return self.load_publish_requests().get(_publish_sk(product_type, product_id))

    def get_publish_request(self, request_id: str) -> Optional[PublishRequest]:
        """Lookup by request id over the loaded records; the sort key is the product, not the id."""
        for req in self.load_publish_requests().values():
            if req.id == request_id:
                return req
        return None

    def save_publish(self, req: PublishRequest, *, after: Collection[Any], refused: str) -> None:
        """Write the request whole on the condition that its row's ``status`` is one of ``after``,
        the states the caller read; another writer's row landing first raises ``refused``."""
        if not self._store.transition(self._publish_to_item(req), {"status": after}):
            raise MarketplaceError(_PUBLISH_MOVED, kind=refused)

    def save(self, sub: Subscription, *, after: Collection[str]) -> None:
        """Write the subscription whole on the condition that its row's ``status`` is one of
        ``after``, the states the caller's guard read; another writer's decision landing first is a
        ``conflict``."""
        if not self._store.transition(self._sub_to_item(sub), {"status": after}):
            raise MarketplaceError(_SUBSCRIPTION_MOVED, kind="conflict")

    def create(self, sub: Subscription) -> bool:
        """Write a new subscription on the condition that its id is new; answer whether it landed."""
        return self._store.put_new(self._sub_to_item(sub))

    def save_listing(self, ptype: ProductType, product_id: str, attrs: Dict[str, Any]) -> Listing:
        """Write ``attrs`` and the key fields alone, so two writers of different fields both land;
        answer the row as written."""
        row = self._store.set_owned(
            (_PARTITION_KEY, _listing_sk(ptype.value, product_id)),
            {**attrs, "_record_kind": _KIND_LISTING, "product_type": ptype.value, "product_id": product_id},
        )
        return self._listing_from_item(row)

    def _scan_partition(self) -> List[dict]:
        return self._store.query(_PARTITION_KEY)

    def _sub_to_item(self, sub: Subscription) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": sub.id,
            "_record_kind": _KIND_SUBSCRIPTION,
            **json.loads(sub.model_dump_json()),
        }

    def _sub_from_item(self, item: dict) -> Subscription:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk", "_record_kind")}
        return Subscription.model_validate(clean)

    def _listing_from_item(self, item: dict) -> Listing:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk", "_record_kind")}
        return Listing.model_validate(clean)

    def _publish_to_item(self, req: PublishRequest) -> dict:
        return {
            "pk": _PARTITION_KEY,
            "sk": _publish_sk(req.product_type, req.product_id),
            "_record_kind": _KIND_PUBLISH,
            **json.loads(req.model_dump_json()),
        }

    def _publish_from_item(self, item: dict) -> PublishRequest:
        clean = {k: v for k, v in item.items() if k not in ("pk", "sk", "_record_kind")}
        return PublishRequest.model_validate(clean)
