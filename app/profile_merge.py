"""Three-way import: only reviewed changes to the extraction baseline are applied."""
from copy import deepcopy


def merge_profile(base, current, reviewed):
    merged = deepcopy(current)
    conflicts = []
    for field, proposed in reviewed.items():
        original, latest = base.get(field), current.get(field)
        if proposed == original:
            continue
        if field in ('skills', 'projects'):
            key = 'name' if field == 'skills' else 'id'
            if any(len({x[key] for x in rows or []}) != len(rows or []) for rows in (original,latest,proposed)):
                conflicts.append(field+'（标识重复，需人工核对）')
                continue
            old = {x[key]:x for x in original or []}
            incoming = {x[key]:x for x in proposed}
            now = {x[key]:x for x in latest or []}
            result = deepcopy(now)
            for identity in dict.fromkeys([*old, *incoming]):
                before, after, present = old.get(identity), incoming.get(identity), now.get(identity)
                if after == before:
                    continue
                if present != before and present != after:
                    conflicts.append(field+'.'+identity)
                elif after is None:
                    result.pop(identity, None)
                else:
                    result[identity] = deepcopy(after)
            merged[field] = list(result.values())
        elif proposed != original:
            if latest != original and latest != proposed:
                conflicts.append(field)
            else:
                merged[field] = deepcopy(proposed)
    return merged, conflicts
