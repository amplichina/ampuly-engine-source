"""A runtime-local talent catalog over already published input bytes.

It exposes only the source templates captured for this generation. Hiring
changes this isolated config in memory; it never imports into the shared
catalog or saves configuration. Existing checkpoints carry owner decisions.
"""
from __future__ import annotations

import json

from ampuly.core.config import TalentTemplateConfig
from ampuly.core.runtime_inputs import RuntimeInputsError


def create_runtime_talent_market(ampuly_home, config, inputs):
    from ampuly.layer2_organization.talent_market import TalentMarket

    if config.org.organization_id != inputs.organization_id:
        raise RuntimeInputsError("runtime_inputs_scope_mismatch")
    try:
        document = json.loads(inputs.files.get("talent/templates.json", b'{"templates":[]}'))
        rows = document["templates"]
        if type(rows) is not list or len(rows) > 64:
            raise ValueError
        templates = [TalentTemplateConfig.model_validate(row) for row in rows]
        if len({row.id.casefold() for row in templates}) != len(templates):
            raise ValueError
        available = {row.id for row in templates}
        selections = inputs.staffing_for('custom', inputs.organization_id).get('staffing_selections', {})
        if type(selections) is not dict:
            raise ValueError
        for selection in selections.values():
            if type(selection) is dict:
                kind = selection.get('kind', selection.get('source', ''))
                template_id = selection.get('id', selection.get('template_id', ''))
            elif type(selection) is str:
                kind, _, template_id = selection.partition(':')
            else:
                raise ValueError
            if kind == 'template' and template_id not in available:
                raise RuntimeInputsError("runtime_template_source_required")
    except (ValueError, KeyError, TypeError, AttributeError):
        raise RuntimeInputsError("runtime_template_unavailable") from None

    class FrozenTalentMarket(TalentMarket):
        def list_available_templates(self):
            return [item.model_copy(deep=True) for item in templates]

        def scan_local_talent(self):
            return self.list_available_templates()

        def import_repo(self, *args, **kwargs):
            raise RuntimeInputsError("runtime_inputs_read_only")

        def _write_prompt(self, *args, **kwargs):
            raise RuntimeInputsError("runtime_inputs_read_only")

    return FrozenTalentMarket(ampuly_home, config)
