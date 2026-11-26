import unittest

from services.onec_data.models import CATALOGS, MOVEMENTS
from services.onec_data.query import EntityQuery, compile_query
from tools import BackendError


GUID = "00000000-0000-0000-0000-000000000001"
SCHEMA = {
    CATALOGS["counterparty"]: {"keys": ["Ref_Key"], "fields": {"Ref_Key": "Edm.Guid", "Description": "Edm.String", "DeletionMark": "Edm.Boolean"}},
    MOVEMENTS: {"keys": ["Recorder", "LineNumber"], "fields": {"Recorder": "Edm.Guid", "LineNumber": "Edm.Int64", "Period": "Edm.DateTime",
        "Организация_Key": "Edm.Guid", "Сумма": "Edm.Double", "Active": "Edm.Boolean"}},
}


class ODataQueryTests(unittest.TestCase):
    def test_json_plan_compiles_guid_date_boolean_and_stable_order(self):
        query = EntityQuery.model_validate_json('''{"entity_name":"AccumulationRegister_ДвиженияДенежныхСредств_RecordType",
            "select":["Сумма"],"sum_field":"Сумма","group_by":["Организация_Key"],
            "filters":[{"field":"Period","operator":"ge","value":"2025-01-01"},
                       {"field":"Организация_Key","value":"00000000-0000-0000-0000-000000000001"},
                       {"field":"Active","value":true}],"order_by":[{"field":"Period","direction":"desc"}],"limit":100}''')
        compiled = compile_query(query, SCHEMA)
        self.assertEqual(compiled.select, "Сумма,Организация_Key,Recorder,LineNumber")
        self.assertEqual(compiled.order_by, "Period desc,Recorder,LineNumber")
        self.assertEqual(compiled.predicate, f"Period ge datetime'2025-01-01T00:00:00' and Организация_Key eq guid'{GUID}' and Active eq true")
        self.assertEqual(compiled.max_rows, 100)

    def test_string_literals_are_escaped_and_or_has_parentheses(self):
        compiled = compile_query({"entity_name": CATALOGS["counterparty"], "select": ["Description"],
            "filters": [{"field": "DeletionMark", "value": False}],
            "any_of": [{"field": "Description", "operator": "contains", "value": "О'БРАЙЕН"},
                       {"field": "Description", "value": "' or true or Description eq '"}]}, SCHEMA)
        self.assertEqual(compiled.predicate,
            "DeletionMark eq false and (substringof('о''брайен',Description) or Description eq ''' or true or Description eq ''')")

    def test_unknown_sources_fields_and_raw_query_text_are_rejected(self):
        valid = {"entity_name": CATALOGS["counterparty"], "select": ["Description"]}
        variants = [
            {**valid, "entity_name": "Catalog_НеРазрешен"}, {**valid, "select": ["Unknown"]},
            {**valid, "filter": "true"}, {**valid, "select": ["Description,$expand=Secret"]},
            {**valid, "filters": [{"field": "Description", "operator": "raw", "value": "true"}]},
            {**valid, "order_by": [{"field": "Unknown"}]}, {**valid, "limit": 0},
            {**valid, "group_by": ["Description"]}, {**valid, "sum_field": "Description"},
        ]
        for query in variants:
            with self.subTest(query=query):
                with self.assertRaises(BackendError):
                    compile_query(query, SCHEMA)
        with self.assertRaises(BackendError):
            compile_query(valid, SCHEMA, allowed_entities=frozenset({MOVEMENTS}))

    def test_values_must_match_schema_types(self):
        for field, value in (("Ref_Key", "not-a-guid"), ("DeletionMark", "false"), ("Description", True)):
            with self.subTest(field=field):
                with self.assertRaises(BackendError):
                    compile_query({"entity_name": CATALOGS["counterparty"], "select": ["Description"],
                                   "filters": [{"field": field, "value": value}]}, SCHEMA)
        for value in (True, "NaN", "Infinity", "1e-999999999", "0e999999999"):
            with self.subTest(value=value):
                with self.assertRaises(BackendError):
                    compile_query({"entity_name": MOVEMENTS, "select": ["Сумма"], "filters": [{"field": "Сумма", "value": value}]}, SCHEMA)
        with self.assertRaises(BackendError):
            compile_query({"entity_name": MOVEMENTS, "select": ["Сумма"], "filters": [{"field": "Period", "value": "2025-01-01T00:00:00+03:00"}]}, SCHEMA)
