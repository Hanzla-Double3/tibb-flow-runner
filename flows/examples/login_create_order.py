from flows.sdk import Call, expect_json, expect_status, flow


@flow(name="login_create_order")
def login_create_order(ctx):
    ctx.data.setdefault("username", "demo")
    ctx.data.setdefault("password", "demo-pass")
    ctx.data.setdefault("items", ["sku-1", "sku-2"])

    ctx.run(Call(
        tool=ctx.api.auth.login,
        args={"username": ctx.data["username"], "password": ctx.data["password"]},
        bind_as="login",
        validate=[
            expect_status(201),
            expect_json("access_token", not_none=True),
        ],
    ))

    ctx.run(Call(
        tool=ctx.api.orders.create_order,
        args={
            "token": lambda ctx: ctx.login.json()["access_token"],
            "items": ctx.data["items"],
        },
        bind_as="order",
        validate=[
            expect_status(200),
            # MCP call used as validation: confirm the order actually landed in the DB.
            Call(
                tool=ctx.api.tester_db.get_order_by_id,
                args={"order_id": lambda ctx: ctx.order.json()["id"]},
                validate=[
                    expect_status(200),
                    expect_json("status", equals="created"),
                ],
            ),
        ],
    ))
