from pydantic import Field

from ._models import AwareDatetime, MetricQuery


class Arguments(MetricQuery):
    as_of: AwareDatetime
    counterparty_ids: list[str] = Field(default_factory=list, max_length=100)
    group_by: list[str] = Field(default_factory=list, max_length=3)


async def execute(runtime, args: Arguments):
    return await runtime.business("get_customer_balances", args.model_dump(mode="json"))
