from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass

from .config import RagVectorConfig, load_rag_vector_config
from .milvus_store import ACTIVE_ALIAS, OUTPUT_FIELDS, collection_name


APP_ROLE = "nanoclaw_rag_rw"
APP_PRIVILEGE_GROUP = "NanoClawVectorStore"
APP_PRIVILEGES = (
    "Search", "Query", "Insert", "Upsert", "Delete", "Flush",
    "Load", "GetLoadState", "DescribeAlias", "DescribeCollection",
)


def _names(values) -> set[str]:
    result: set[str] = set()
    for value in values or ():
        if isinstance(value, str):
            result.add(value)
        elif isinstance(value, dict):
            for key in ("user_name", "username", "role_name", "role"):
                if value.get(key):
                    result.add(str(value[key]))
    return result


def _new_client(uri: str, token: str, database: str, timeout: int):
    from pymilvus import MilvusClient
    return MilvusClient(uri=uri, token=token, db_name=database, timeout=timeout)


def bootstrap_auth(*, uri: str, root_password: str, app_user: str, app_password: str,
                   database: str = "nanoclaw_vector_docker", timeout: int = 5,
                   client_factory=_new_client) -> dict[str, object]:
    if not all((uri, root_password, app_user, app_password, database)):
        raise ValueError("missing Milvus bootstrap configuration")
    root_token = f"root:{root_password}"
    root = None
    rotated = False
    try:
        candidate = client_factory(uri, root_token, "default", timeout)
        candidate.list_databases()
        root = candidate
    except Exception:
        initial = client_factory(uri, "root:Milvus", "default", timeout)
        try:
            initial.list_databases()
            initial.update_password("root", "Milvus", root_password, reset_connection=True)
            rotated = True
        finally:
            initial.close()
        root = client_factory(uri, root_token, "default", timeout)
        root.list_databases()

    try:
        if database not in root.list_databases():
            root.create_database(database)
    finally:
        root.close()

    admin = client_factory(uri, root_token, database, timeout)
    try:
        users = _names(admin.list_users())
        if app_user not in users:
            admin.create_user(app_user, app_password)
        roles = _names(admin.list_roles())
        if APP_ROLE not in roles:
            admin.create_role(APP_ROLE)
        admin.grant_role(app_user, APP_ROLE)
        groups = {item.get("privilege_group") for item in admin.list_privilege_groups()}
        if APP_PRIVILEGE_GROUP not in groups:
            admin.create_privilege_group(APP_PRIVILEGE_GROUP)
            admin.add_privileges_to_group(APP_PRIVILEGE_GROUP, list(APP_PRIVILEGES))
        admin.grant_privilege_v2(APP_ROLE, APP_PRIVILEGE_GROUP, "*", db_name=database)
        admin.grant_privilege_v2(APP_ROLE, "DatabaseReadOnly", "*", db_name=database)
    finally:
        admin.close()

    app = client_factory(uri, f"{app_user}:{app_password}", database, timeout)
    try:
        app.list_collections()
    except Exception as exc:
        raise RuntimeError("Milvus application identity verification failed") from exc
    finally:
        app.close()
    return {"ready": True, "root_password_rotated": rotated, "database": database,
            "application_role": APP_ROLE}


@dataclass
class MilvusCollectionManager:
    client: object
    dimensions: int = 64

    def _schema(self):
        from pymilvus import DataType, MilvusClient
        schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
        varchar = {
            "child_id": 512, "document_id": 512, "parent_id": 512,
            "child_location": 2048, "child_content_hash": 128,
            "source_uri": 4096, "source_title": 2048, "source_content_hash": 128,
            "content_type": 256, "source_location": 2048, "language": 32,
            "business_unit_id": 256, "classification": 64,
            "document_status": 32, "parser_version": 128,
            "embedding_model_id": 256,
        }
        schema.add_field("child_id", DataType.VARCHAR, is_primary=True, max_length=512)
        for name, max_length in varchar.items():
            if name != "child_id":
                schema.add_field(name, DataType.VARCHAR, max_length=max_length)
        schema.add_field("document_version", DataType.INT64)
        schema.add_field("child_text", DataType.VARCHAR, max_length=65535)
        schema.add_field("child_metadata", DataType.JSON)
        schema.add_field("allowed_roles", DataType.ARRAY, element_type=DataType.VARCHAR,
                         max_capacity=64, max_length=256)
        schema.add_field("is_public", DataType.BOOL)
        schema.add_field("expires_at_epoch_ms", DataType.INT64)
        schema.add_field("source_metadata", DataType.JSON)
        schema.add_field("updated_at_epoch_ms", DataType.INT64)
        schema.add_field("embedding", DataType.FLOAT_VECTOR, dim=self.dimensions)
        return schema

    def create_generation(self, generation: int) -> str:
        from pymilvus import MilvusClient
        name = collection_name(generation)
        if self.client.has_collection(name):
            raise ValueError("Milvus generation already exists")
        indexes = MilvusClient.prepare_index_params()
        indexes.add_index("embedding", index_type="AUTOINDEX", metric_type="COSINE",
                          index_name="embedding_cosine")
        self.client.create_collection(
            name, schema=self._schema(), index_params=indexes,
            consistency_level="Strong",
        )
        self.client.load_collection(name)
        return name

    def activate_generation(self, generation: int) -> dict[str, str | None]:
        name = collection_name(generation)
        if not self.client.has_collection(name):
            raise ValueError("Milvus generation does not exist")
        previous = None
        try:
            alias = self.client.describe_alias(ACTIVE_ALIAS)
            previous = alias.get("collection_name") or alias.get("collection")
            self.client.alter_alias(name, ACTIVE_ALIAS)
        except Exception as exc:
            message = str(exc).lower()
            if "alias" not in message or not any(word in message for word in ("not found", "not exist")):
                raise
            self.client.create_alias(name, ACTIVE_ALIAS)
        return {"active": name, "previous": previous}

    def drop_generation(self, generation: int) -> str:
        name = collection_name(generation)
        try:
            alias = self.client.describe_alias(ACTIVE_ALIAS)
            active = alias.get("collection_name") or alias.get("collection")
        except Exception:
            active = None
        if active == name:
            raise ValueError("cannot drop the active Milvus generation")
        if not self.client.has_collection(name):
            raise ValueError("Milvus generation does not exist")
        self.client.drop_collection(name)
        return name

    def check(self) -> dict[str, object]:
        alias = self.client.describe_alias(ACTIVE_ALIAS)
        target = alias.get("collection_name") or alias.get("collection")
        description = self.client.describe_collection(target)
        field_names = {field.get("name") for field in description.get("fields", [])}
        missing = sorted(set(OUTPUT_FIELDS + ["embedding", "is_public", "updated_at_epoch_ms"])
                         - field_names)
        if missing:
            raise RuntimeError("Milvus schema is missing required fields")
        state = self.client.get_load_state(target)
        return {"ready": True, "active_collection": target,
                "load_state": str(state.get("state", ""))}


def _admin_client():
    uri = os.environ.get("RAG_MILVUS_URI", "").strip()
    token = os.environ.get("RAG_MILVUS_ADMIN_TOKEN", "")
    database = os.environ.get("RAG_MILVUS_DATABASE", "nanoclaw_vector_docker").strip()
    timeout = int(os.environ.get("RAG_MILVUS_CONNECT_TIMEOUT_SECONDS", "5"))
    if not uri or not token or not database:
        raise ValueError("missing Milvus admin configuration")
    return _new_client(uri, token, database, timeout)


def main() -> int:
    parser = argparse.ArgumentParser(description="Explicit M3 Milvus administration")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--bootstrap-auth", action="store_true")
    actions.add_argument("--check", action="store_true")
    actions.add_argument("--create-generation", type=int)
    actions.add_argument("--activate-generation", type=int)
    actions.add_argument("--rollback-generation", type=int)
    actions.add_argument("--drop-generation", type=int)
    args = parser.parse_args()

    if args.bootstrap_auth:
        result = bootstrap_auth(
            uri=os.environ.get("RAG_MILVUS_URI", "").strip(),
            root_password=os.environ.get("DOCKER_MILVUS_ROOT_PASSWORD", ""),
            app_user=os.environ.get("DOCKER_MILVUS_APP_USER", ""),
            app_password=os.environ.get("DOCKER_MILVUS_APP_PASSWORD", ""),
            database=os.environ.get("RAG_MILVUS_DATABASE", "nanoclaw_vector_docker").strip(),
            timeout=int(os.environ.get("RAG_MILVUS_CONNECT_TIMEOUT_SECONDS", "5")),
        )
    elif args.check:
        config: RagVectorConfig = load_rag_vector_config()
        from .milvus_store import MilvusStore
        MilvusStore(config).check_ready()
        result = {"ready": True, "active_alias": ACTIVE_ALIAS}
    else:
        client = _admin_client()
        try:
            manager = MilvusCollectionManager(client)
            if args.create_generation is not None:
                result = {"created": manager.create_generation(args.create_generation)}
            elif args.activate_generation is not None:
                result = manager.activate_generation(args.activate_generation)
            elif args.rollback_generation is not None:
                result = manager.activate_generation(args.rollback_generation)
            else:
                result = {"dropped": manager.drop_generation(args.drop_generation)}
        finally:
            client.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
