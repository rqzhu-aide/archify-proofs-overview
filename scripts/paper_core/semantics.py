"""Small adapters for the SQL superset's canonical optional extensions.

These work with a Database, a prospective State, or a historical Snapshot.
Stored shared uses never duplicate their application's mathematical fields.
"""
from __future__ import annotations

from copy import deepcopy

from .refs import referrers


def live(state, collection, identity, revision=None):
    if revision is not None:
        db = getattr(state, 'db', state)
        record = db.latest_at(collection, identity, revision)
    elif hasattr(state, 'live'):
        record = state.live(collection, identity)
    else:
        record = state.head(collection, identity)
    return record if record is not None and not record.retired else None


def related(state, collection, field, target, *, prefix=False, revision=None):
    # Prospective batches must include records created in that same batch.
    if hasattr(state, 'overlay'):
        keys = {(c, i) for c, i, path in state.referrers(target['collection'], target['id'])
                if c == collection and (path.startswith(field + '/') if prefix else path == field)}
        return [r for c, i in sorted(keys) if (r := state.live(c, i)) is not None]
    db = getattr(state, 'db', state)
    revision = revision if revision is not None else getattr(state, 'revision', None)
    keys = referrers(db.conn, (collection,), field, [(target['collection'], target['id'])],
                     prefix=prefix, revision=revision)
    return [r for c, i, _ in keys if (r := live(state, c, i, revision)) is not None]


def application(state, use, revision=None):
    if isinstance(use, str):
        use = live(state, 'uses', use, revision)
    if use is None:
        return {'use_id': None, 'group_id': None, 'needed_form': None, 'substitutions': [],
                'scope_id': None, 'state': 'draft'}
    detail = live(state, 'application_details', use.id, revision)
    if detail is not None:
        return dict(detail.body, scope_id=detail.body.get('scope_id'))
    # Historical version-3 bodies remain interpretable; a version-4 summary has
    # no such fields and receives no application credit.
    body = use.body
    return {'use_id': use.id, 'group_id': body.get('group_id'), 'needed_form': body.get('needed_form'),
            'substitutions': body.get('substitutions', []), 'scope_id': None,
            'state': 'registered' if body.get('group_id') and body.get('needed_form') else 'draft'}


def target_spec(state, target, revision=None):
    records = related(state, 'target_specs', '/target', target, revision=revision)
    return records[0] if len(records) == 1 else None


def exact_scope(state, target, revision=None):
    spec = target_spec(state, target, revision)
    if spec is not None:
        return spec.body['scope_id']
    record = live(state, target['collection'], target['id'], revision)
    return record.body.get('scope_id') if record else None


def exact_statement(state, target, revision=None):
    spec = target_spec(state, target, revision)
    if spec is None or spec.body['state'] != 'registered':
        return None
    if spec.body['statement'] is not None:
        return spec.body['statement']
    pin = spec.body['statement_ref']
    record = state.version(pin['collection'], pin['id'], pin['version'])
    return record.body['statement'] if record and not record.retired else None


def boundaries(state, argument_id, revision=None):
    return related(state, 'proof_boundaries', '/argument_ids', {'collection': 'arguments', 'id': argument_id},
                   prefix=True, revision=revision)


def normalize_edits(db, edits):
    """Accept the established authoring shape without retaining duplicate fields.

    A shared-only overview update leaves the extension untouched. Explicit old
    application fields are converted once into a separately versioned extension.
    """
    result = deepcopy(edits)
    explicit = {(e['collection'], e['id']) for e in result}
    additions = []
    for edit in result:
        if edit['collection'] == 'uses' and edit['op'] == 'retire' and ('application_details',edit['id']) not in explicit:
            prior = db.head('application_details',edit['id'])
            if prior is not None and not prior.retired:
                additions.append({'op':'retire','collection':'application_details','id':edit['id'],
                                  'expected_version':prior.version,'reason':edit['reason']})
        body = edit.get('body')
        if edit['collection'] == 'items' and isinstance(body, dict):
            for key, value in (('origin', 'source'), ('owner_id', None), ('scope_id', None)):
                body.setdefault(key, value)
        if edit['collection'] != 'uses' or not isinstance(body, dict):
            continue
        fields = ('group_id', 'needed_form', 'substitutions')
        if not any(k in body for k in fields):
            continue
        detail = {k: body.pop(k, [] if k == 'substitutions' else None) for k in fields}
        if ('application_details', edit['id']) in explicit:
            from .errors import InvalidRequest
            raise InvalidRequest('specify application_details or legacy application fields, not both')
        prior = db.head('application_details', edit['id'])
        if not any(detail.values()) and prior is None:
            continue
        detail.update(use_id=edit['id'], scope_id=None,
                      state='registered' if detail['group_id'] and detail['needed_form'] else 'draft')
        if prior is not None and not prior.retired and prior.body == detail:
            continue
        additions.append({'op': 'replace' if prior else 'create', 'collection': 'application_details',
                          'id': edit['id'], 'expected_version': prior.version if prior else None, 'body': detail})
    return result + additions
