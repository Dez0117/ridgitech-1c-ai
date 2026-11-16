from getpass import getpass
import os

import uvicorn
from dotenv import load_dotenv


if __name__ == "__main__":
    load_dotenv(override=False)
    os.environ.setdefault("ODATA_BASE_URL", "https://176.108.255.199.sslip.io/Itmo/odata/standard.odata")
    os.environ.setdefault("ODATA_USERNAME", "odata")
    if not os.environ.get("ODATA_PASSWORD"):
        os.environ["ODATA_PASSWORD"] = getpass("Пароль OData (не сохраняется): ")
    uvicorn.run("services.onec_data.main:app", host="127.0.0.1", port=8011)
