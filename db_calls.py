import re
import logging

async def extract_gk(slots, rpc_client):
    used_params = {param_name: param_value for param_name, param_value in slots.items()
                   if param_value != "не имеет значения"}
    catalogs = await rpc_client.call({"extract_catalog": used_params})
    context = []
    for catalog in catalogs[:3]:
        catalog_descr = []
        for param_name, param_value in catalog.items():
            if param_name != "Фото":
                catalog_descr.append(f"{param_name}: {param_value}")
        catalog_descr = "\n".join(catalog_descr)
        context.append(catalog_descr)
    context = "\n\n".join(context).strip()
    return context, catalog
#r