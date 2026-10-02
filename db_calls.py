from collections.abc import Mapping
from catalog_filters import normalize_filters

async def extract_gk(slots, rpc_client):
    if not isinstance(slots, Mapping):
        raise ValueError("Catalog filters must be an object")
    used_params = normalize_filters(slots)
    catalogs = await rpc_client.call({"extract_catalog": used_params})
    if not isinstance(catalogs, list) or not all(isinstance(item, dict) for item in catalogs):
        raise ValueError("Catalog response must be a list of objects")
    if not catalogs:
        return "", None
    context = []
    for catalog in catalogs[:3]:
        catalog_descr = []
        for param_name, param_value in catalog.items():
            if param_name != "Фото":
                catalog_descr.append(f"{param_name}: {param_value}")
        catalog_descr = "\n".join(catalog_descr)
        context.append(catalog_descr)
    context = "\n\n".join(context).strip()
    return context, catalogs[0]
#r
