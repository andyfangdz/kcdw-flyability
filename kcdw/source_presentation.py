"""Descriptions of already-validated model data; not a data validator.

Callers must validate the source first. Never infer direct sourcing from a model
name, retrieval timestamp, or another packet's initialization.
"""


def is_direct(data):
    meta = data.get('metadata', {}) if isinstance(data, dict) else {}
    return (isinstance(meta, dict) and meta.get('provenance') == 'direct-native'
            and meta.get('model_init_is_response_bound') is True
            and meta.get('source_provider') in ('NOAA', 'ECMWF', 'ECCC'))


def source_description(data):
    """Plain text, escaped by its HTML caller; old archives stay rolling."""
    meta = data.get('metadata', {})
    if is_direct(data):
        return (f"{meta['source_provider']} direct · verified run {meta['initialization_time']}. "
                + meta.get('sampling', 'Native samples; display interpolation adds no timing skill.'))
    label = 'Open-Meteo fallback' if meta.get('direct_fallback_reason') else 'Open-Meteo'
    return label + ' · Rolling values; advertised initialization is not response-bound.'


SOURCE_LICENSES = ('NOAA/NCEP native data are public domain; ECMWF native data: CC BY 4.0. '
                   'Open-Meteo fallback data: CC BY 4.0. Derived guidance is not an official aviation forecast.')
