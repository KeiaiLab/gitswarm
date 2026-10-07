"""vulture 허용 목록 — 코드가 부르지 않지만 살아 있는 이름. 이름마다 이유 한 줄.

새 이름을 더하기 전에: 정말 죽은 코드면 지운다. 여기는 데코레이터 등록·console script 로만 쓰이는 것뿐이다.
`uv run vulture src vulture_whitelist.py --min-confidence 60`
"""

from gitswarm.surfaces import cli, mcp

# ── typer: @app / @hive_app / @ws_app / @events_app .command 가 등록한다 ──
cli.hive_init  # gitswarm hive init
cli.ws_create  # gitswarm ws create
cli.ws_get  # gitswarm ws get
cli.ws_list  # gitswarm ws list
cli.ws_read  # gitswarm ws read
cli.ws_tree  # gitswarm ws tree
cli.ws_publish  # gitswarm ws publish
cli.ws_drop  # gitswarm ws drop
cli.ws_gc  # gitswarm ws gc
cli.events_tail  # gitswarm events tail
cli.doctor  # gitswarm doctor
cli.stats  # gitswarm stats
cli.mcp_serve  # gitswarm mcp
cli.main  # [project.scripts] gitswarm = "gitswarm.surfaces.cli:main"

# ── fastmcp: @mcp.tool 가 MCP 도구로 등록한다 ──
mcp.hive_init  # tool hive_init
mcp.workspace_create  # tool workspace_create
mcp.workspace_get  # tool workspace_get
mcp.workspace_list  # tool workspace_list
mcp.workspace_read_file  # tool workspace_read_file
mcp.workspace_tree  # tool workspace_tree
mcp.workspace_publish  # tool workspace_publish
mcp.workspace_drop  # tool workspace_drop
mcp.workspace_gc  # tool workspace_gc
mcp.events_tail  # tool events_tail
mcp.doctor  # tool doctor
mcp.stats  # tool stats
