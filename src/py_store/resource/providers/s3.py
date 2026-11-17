"""S3 兼容 provider（OSS / MinIO / AWS 共用）；boto3 懒加载（可选依赖）。"""

import asyncio
from typing import Any, cast


def create(options: dict[str, Any] | None = None) -> dict[str, Any]:
    """构造 S3 provider（``bucket`` 必填；``client`` 可注入以复用既有 boto3 客户端）。"""
    options = options or {}
    import boto3  # 懒加载：未安装时仅该 provider 报错，不影响其他

    bucket = options.get("bucket")
    if not bucket:
        raise ValueError("s3 provider 需要 options.bucket")

    client = options.get("client")
    if client is None:
        endpoint = options.get("endpoint")
        force_path = options.get("forcePathStyle")
        if force_path is None:
            force_path = bool(endpoint)
        addressing = "path" if force_path else "virtual"
        creds = options.get("credentials") or {}
        client = boto3.client(
            "s3",
            region_name=options.get("region") or "us-east-1",
            endpoint_url=endpoint,
            aws_access_key_id=creds.get("accessKeyId"),
            aws_secret_access_key=creds.get("secretAccessKey"),
            config=_boto_config(addressing),
        )

    prefix = options.get("prefix")

    def key_of(key: str) -> str:
        """拼对象完整键（配置 ``prefix`` 时加前缀）。"""
        if prefix:
            return f"{str(prefix).rstrip('/')}/{key}"
        return key

    def _put(key: str, data: bytes, opts: dict[str, Any] | None) -> None:
        """同步 put_object（``opts.mime`` 作为 ContentType）。"""
        client.put_object(
            Bucket=bucket,
            Key=key_of(key),
            Body=data,
            ContentType=(opts or {}).get("mime") or "application/octet-stream",
        )

    def _get(key: str) -> bytes:
        """同步 get_object 并读出全部字节。"""
        r = client.get_object(Bucket=bucket, Key=key_of(key))
        return cast(bytes, r["Body"].read())

    def _remove(key: str) -> None:
        """同步 delete_object。"""
        client.delete_object(Bucket=bucket, Key=key_of(key))

    def _exists(key: str) -> bool:
        """同步 head_object 探测存在性（404/NoSuchKey 记不存在，其余错误原样抛出）。"""
        try:
            client.head_object(Bucket=bucket, Key=key_of(key))
            return True
        except Exception as e:
            response = getattr(e, "response", {}) or {}
            status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
            err_code = (response.get("Error") or {}).get("Code")
            if status == 404 or err_code in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    async def put(key: str, data: bytes, opts: dict[str, Any] | None = None) -> None:
        """写入对象（阻塞调用经线程池）。"""
        await asyncio.to_thread(_put, key, data, opts)

    async def get(key: str, opts: dict[str, Any] | None = None) -> bytes:
        """读取对象字节。"""
        return await asyncio.to_thread(_get, key)

    async def remove(key: str, opts: dict[str, Any] | None = None) -> None:
        """删除对象。"""
        await asyncio.to_thread(_remove, key)

    async def exists(key: str, opts: dict[str, Any] | None = None) -> bool:
        """判断对象是否存在。"""
        return await asyncio.to_thread(_exists, key)

    return {
        "kind": options.get("kind", "s3"),
        "put": put,
        "get": get,
        "remove": remove,
        "exists": exists,
    }


def _boto_config(addressing: str) -> Any:
    """构造 botocore ``Config``（仅设置 S3 寻址风格：path / virtual）。"""
    from botocore.config import Config

    return Config(s3={"addressing_style": addressing})
