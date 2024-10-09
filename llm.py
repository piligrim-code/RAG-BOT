import aiohttp
import json
import logging
import re
"""messages = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "some text"},
    {"role": "assistant", "content": "some text"},
    {"role": "user", "content": "some text"},
    ...
]
"""

EXTRACT_INSTRUCTION = """Определи следующие параметры из реплики покупателя:
1) артикул.
2) бренд товара.
3) описание.
4) ценовую категорию.
Если пользователь говорит, что не имеет значения (без разницы), какой тип, выведи параметр: не имеет значения.
Ответ выведи в формате JSON.
Пример реплики покупателя: Мне нужен диск по бетону HILBERG до 100000
Ответ: {"бренд": "HILBERG", описание: "диск по бетону" "цена": {"<": 100000}}
Пример реплики покупателя: Расскажи про все алмазные диски по металлу 
Ответ: {описание: "алмазный диск по металлу"}
Пример реплики покупателя: В чем различия между диском пильным HA216 и диском алмазным HS102
Ответ: {артикул: "HS102, HA216", "описание: "алмазный диск, пильный диск "}

Пример реплики покупателя: """

PROMPT_EXTRACT_TEMPLATE = """{instruction}
{replicas}
Ответ: """


async def slot_fill(user_data, message):
    messages = user_data.get("messages", [])
    param_dict = user_data.get("catalog_params", {})
    last_messages = []
    if messages:
        last_messages.append(f"Реплика консультанта: {messages[-1]}")
    last_messages.append(f"Реплика покупателя: {message.text}")
    prompt = PROMPT_EXTRACT_TEMPLATE.format(instruction=EXTRACT_INSTRUCTION, replicas="\n".join(last_messages))
    async with aiohttp.ClientSession() as session:
        response = await session.post("http://localhost:8015/generate", json={"content": prompt})
        response = await response.json()

    with open("/data/log.txt", 'a') as out:
        out.write(f"slot_fill, response: {response}"+'\n')

    lines = response["res_content"].split("\n")
    lines = [line.strip() for line in lines]
    response = " ".join(lines)
    for old_symb, new_symb in [("{ ", "{"), (" }", "}")]:
        response = response.replace(old_symb, new_symb)
    fnd = re.findall(r"```json (.*?) ```", response)
    new_param_dict = {}
    if fnd:
        try:
            new_param_dict = json.loads(fnd[0])
        except Exception as e:
            logging.error(f"Error in parameter loading: {e}")
    else:
        try:
            new_param_dict = json.loads(response)
        except Exception as e:
            logging.error(f"Error in parameter loading: {e}")
    for param_name, param_value in new_param_dict.items():
        param_dict[param_name] = param_value
    with open("/data/log.txt", 'a') as out:
        out.write(f"slot_fill, param_dict: {param_dict}"+'\n')
    return param_dict
#r