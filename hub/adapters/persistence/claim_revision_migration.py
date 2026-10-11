"""Repair the legacy re-authorization revision reset before readers start.

The event stream position, event ID, DeviceRef and authority decisions never
change. Only the broken ordering metadata is repaired, in the schema startup
transaction, together with its outbox copy and current Claim. Valid histories
are untouched; rerunning is a no-op. Consumers can resume their existing cursor.
"""
import json
import logging
from sqlalchemy import select, update
from hub.adapters.persistence.models import (
    AdmissionClaimEventStreamRow as Stream,
    AdmissionOutboxRow as Outbox,
    AdmissionClaimRow as Claim,
)


def repair_claim_revisions(connection):
    histories = {}
    for row in connection.execute(select(Stream.__table__).order_by(Stream.stream_position)).mappings():
        event = json.loads(row['event_json'])
        histories.setdefault((event['ownerdomainid'], event['subject']), []).append((row, event))
    repaired = 0
    for history in histories.values():
        previous = 0
        previous_generation = None
        changes = []
        offset = 0
        for row, event in history:
            revision = event['aggregaterev']
            ref = event['data']['device_ref']
            generation = (ref['owner_domain_generation'], ref['claim_generation'], ref['trust_epoch'])
            candidate = revision + offset
            if candidate <= previous:
                # This migration has evidence for one historical producer bug,
                # not permission to reinterpret arbitrary corrupt event ordering.
                if not (event['type'] == 'live.eidolon.device.claim-activated.v1'
                        and revision == 1 and previous_generation is not None
                        and generation > previous_generation):
                    raise RuntimeError('Claim revision history requires explicit recovery')
                offset = previous
                candidate = revision + offset
            if candidate != revision:
                changes.append((row, event, candidate))
            previous = candidate
            previous_generation = generation
        if not changes:
            continue
        latest = history[-1][1]
        device_id = latest['data']['device_ref']['device_instance_id']
        claim = connection.execute(select(Claim.__table__).where(Claim.device_instance_id == device_id)).mappings().one()
        if claim['revision'] != latest['aggregaterev']:
            raise RuntimeError('Claim revision and event history disagree; refusing repair')
        for row, event, revision in changes:
            outbox = connection.execute(select(Outbox.__table__).where(Outbox.event_id == row['event_id'])).mappings().one()
            if json.loads(outbox['event_json']) != event:
                raise RuntimeError('Claim outbox and stream disagree; refusing repair')
            event['aggregaterev'] = revision
            encoded = json.dumps(event, sort_keys=True)
            connection.execute(update(Stream).where(Stream.event_id == row['event_id']).values(event_json=encoded))
            connection.execute(update(Outbox).where(Outbox.event_id == row['event_id']).values(event_json=encoded, aggregate_revision=revision))
            repaired += 1
        connection.execute(update(Claim).where(Claim.device_instance_id == device_id).values(revision=previous))
    if repaired:
        logging.getLogger(__name__).warning('Repaired %s legacy Claim event revisions', repaired)
    return repaired
