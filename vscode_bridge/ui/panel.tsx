import {
  Page,
  Card,
  Grid,
  StatCard,
  Stack,
  Text,
  Button,
  Toolbar,
  ToolbarGroup,
  StatusBadge,
  KeyValue,
  DataTable,
  Progress,
  Alert,
  EmptyState,
  SegmentedControl,
  Switch,
  Textarea,
  NumberInput,
  Field,
  CodeBlock,
  Divider,
  useToast,
  useEffect,
  useState,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps, Tone } from "@neko/plugin-ui"

type PanelState = Record<string, any>

export default function Panel(props: PluginSurfaceProps<PanelState>) {
  const t = (key: string, params?: Record<string, any>) => props.t(key, params)
  const state = (props.state || {}) as PanelState
  const server = (state.server || {}) as PanelState
  const workspaces = Array.isArray(state.workspaces) ? state.workspaces : []
  const model = (state.coding_model || {}) as PanelState
  const settings = (state.settings || {}) as PanelState
  const task = state.task as PanelState | null
  const history = Array.isArray(state.history) ? state.history : []
  const toast = useToast()

  const [tab, setTab] = useState("overview")
  const [modelMode, setModelMode] = useState(String(settings.model_mode || "auto"))
  const [permissionMode, setPermissionMode] = useState(String(settings.permission_mode || "ask"))
  const [allowTerminal, setAllowTerminal] = useState(settings.allow_terminal !== false)
  const [takeoverMode, setTakeoverMode] = useState(String(settings.coding_takeover_mode || "auto"))
  const [maxSteps, setMaxSteps] = useState(Number(settings.max_steps) || 0)
  const [taskTimeoutMinutes, setTaskTimeoutMinutes] = useState(
    Math.round((Number(settings.task_timeout_seconds) || 0) / 60),
  )
  const [modelTimeoutSeconds, setModelTimeoutSeconds] = useState(
    Number(settings.model_timeout_seconds) || 1800,
  )
  const [directRequest, setDirectRequest] = useState("")
  const [busy, setBusy] = useState("")

  useEffect(() => {
    const interval = task ? 1000 : 2000
    const timer = setInterval(() => {
      void props.api.refresh()
    }, interval)
    return () => clearInterval(timer)
  }, [task?.task_id, task?.status])

  useEffect(() => {
    setModelMode(String(settings.model_mode || "auto"))
    setPermissionMode(String(settings.permission_mode || "ask"))
    setAllowTerminal(settings.allow_terminal !== false)
    setTakeoverMode(String(settings.coding_takeover_mode || "auto"))
    setMaxSteps(Number(settings.max_steps) || 0)
    setTaskTimeoutMinutes(Math.round((Number(settings.task_timeout_seconds) || 0) / 60))
    setModelTimeoutSeconds(Number(settings.model_timeout_seconds) || 1800)
  }, [
    settings.model_mode,
    settings.permission_mode,
    settings.allow_terminal,
    settings.coding_takeover_mode,
    settings.max_steps,
    settings.task_timeout_seconds,
    settings.model_timeout_seconds,
  ])

  const call = async (id: string, args?: Record<string, any>) => {
    setBusy(id)
    try {
      const result = await props.api.call(id, args)
      toast.success(t("panel.actionDone", { action: id }))
      await props.api.refresh()
      return result
    } catch (error) {
      const message = error && (error as any).message ? (error as any).message : String(error)
      toast.error(message)
      return null
    } finally {
      setBusy("")
    }
  }

  const saveSettings = async () => {
    await call("save_settings", {
      settings: {
        model_mode: modelMode,
        permission_mode: permissionMode,
        allow_terminal: allowTerminal,
        coding_takeover_mode: takeoverMode,
        max_steps: maxSteps,
        task_timeout_seconds: Math.round(taskTimeoutMinutes * 60),
        model_timeout_seconds: modelTimeoutSeconds,
      },
    })
  }

  const startDirectTask = async () => {
    const request = directRequest.trim()
    if (!request) {
      toast.error(t("panel.direct.empty"))
      return
    }
    const result = await call("start_coding_task", { request })
    const payload = result && (result as any).data ? (result as any).data : result
    if (payload && (payload as any).task_id) {
      setDirectRequest("")
      setTab("tasks")
      toast.success(t("panel.direct.started", { task: (payload as any).task_id }))
    }
  }

  const testModel = async () => {
    setBusy("test_coding_model")
    try {
      const result = await props.api.call("test_coding_model")
      const payload = result && result.data ? result.data : result
      if (payload && payload.ok === false) {
        toast.error(String(payload.error || t("panel.test.failed")))
      } else {
        toast.success(t("panel.test.ok"))
      }
      await props.api.refresh()
    } catch (error) {
      const message = error && (error as any).message ? (error as any).message : String(error)
      toast.error(message)
    } finally {
      setBusy("")
    }
  }

  const modelStatus = String(model.status || "unknown")
  const modelReady = Boolean(model.ready)
  const modelStatusLabel = t(`panel.model.status.${modelStatus}`)
  const tokenPreview = String(server.token_preview || "—")
  const discoveryPath = String(server.discovery_path || server.discovery_dir || "—")
  const modelLink = `${props.host?.origin || ""}${(state.links && state.links.model_api) || "/model-api?plugin_id=vscode_bridge"}`
  const taskStatus = task ? String(task.status || "running") : ""
  const taskStatusLabel = (status: string) => t(`panel.task.status.${status}`)
  const taskStatusTone = (status: string): Tone => {
    if (status === "completed") {
      return "success"
    }
    if (status === "failed" || status === "cancelled") {
      return "danger"
    }
    if (status === "waiting_approval") {
      return "warning"
    }
    return "info"
  }
  const permissionDescription =
    permissionMode === "ask"
      ? t("panel.permission.askDesc")
      : permissionMode === "auto_edit"
        ? t("panel.permission.auto_editDesc")
        : t("panel.permission.full_accessDesc")
  const permissionTone: Tone =
    permissionMode === "full_access"
      ? "danger"
      : permissionMode === "auto_edit"
        ? "warning"
        : "info"
  const taskProgress = task
    ? Math.round((Number(task.step) || 0) / Math.max(1, Number(task.max_steps) || 1) * 100)
    : 0
  const phase = task ? String(task.phase || task.status || "") : ""
  const phaseLabel = phase ? t(`panel.phase.${phase}`) : ""
  const activity = task && Array.isArray(task.activity) ? task.activity : []
  const formatDuration = (seconds: number) => {
    const total = Math.max(0, Math.floor(Number(seconds) || 0))
    const h = Math.floor(total / 3600)
    const m = Math.floor((total % 3600) / 60)
    const s = total % 60
    if (h > 0) {
      return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
    }
    return `${m}:${String(s).padStart(2, "0")}`
  }
  const formatClock = (at: number) => {
    const date = new Date(Number(at) * 1000)
    return Number.isFinite(date.getTime()) ? date.toLocaleTimeString() : "—"
  }
  const activityColumns = [
    {
      key: "time",
      label: t("panel.task.time"),
      render: (row: PanelState) => formatClock(Number(row.at) || 0),
    },
    {
      key: "phase",
      label: t("panel.task.phase"),
      render: (row: PanelState) => t(`panel.phase.${String(row.phase || "")}`),
    },
    {
      key: "tool",
      label: t("panel.task.tool"),
      render: (row: PanelState) => String(row.tool || "—"),
    },
    {
      key: "message",
      label: t("panel.task.message"),
      render: (row: PanelState) => String(row.message || ""),
    },
    {
      key: "result",
      label: t("panel.task.result"),
      render: (row: PanelState) => {
        if (row.ok === true) {
          return t("panel.task.resultOk")
        }
        if (row.ok === false) {
          return t("panel.task.resultFailed")
        }
        return String(row.detail || "—")
      },
    },
  ]

  const workspaceColumns = [
    { key: "name", label: t("panel.workspace.name") },
    {
      key: "roots",
      label: t("panel.workspace.roots"),
      render: (row: PanelState) => Array.isArray(row.roots) ? row.roots.join(", ") : "",
    },
    {
      key: "active",
      label: t("panel.workspace.active"),
      render: (row: PanelState) => row.active ? t("panel.connection.active") : "—",
    },
    {
      key: "trusted",
      label: t("panel.workspace.trusted"),
      render: (row: PanelState) => row.is_trusted ? t("panel.workspace.yes") : t("panel.workspace.no"),
    },
    {
      key: "extension",
      label: t("panel.workspace.extension"),
      render: (row: PanelState) => String(row.extension_version || "—"),
    },
    {
      key: "action",
      label: "",
      render: (row: PanelState) => (
        <Button onClick={() => call("select_workspace", { workspace_id: row.workspace_id })}>
          {t("panel.connection.useWorkspace")}
        </Button>
      ),
    },
  ]

  const historyColumns = [
    { key: "task_id", label: t("panel.task.taskId") },
    {
      key: "status",
      label: t("panel.task.status"),
      render: (row: PanelState) => taskStatusLabel(String(row.status || "")),
    },
    {
      key: "summary",
      label: t("panel.task.summary"),
      render: (row: PanelState) => String(row.summary || row.error || "—"),
    },
    {
      key: "files_count",
      label: t("panel.task.filesCount"),
      render: (row: PanelState) => t("panel.task.filesCountValue", {
        count: Array.isArray(row.files_changed) ? row.files_changed.length : 0,
      }),
    },
  ]

  return (
    <Page title={t("panel.title")} subtitle={t("panel.subtitle")}>
      <Stack>
        <Toolbar>
          <ToolbarGroup>
            <StatusBadge
              tone={server.running ? "success" : "danger"}
              label={server.running ? t("panel.connection.running") : t("panel.connection.stopped")}
            />
            <StatusBadge
              tone={workspaces.length > 0 ? "info" : "warning"}
              label={t("panel.status.workspaces", { count: workspaces.length })}
            />
            <StatusBadge
              tone={modelReady ? "success" : model.status === "incompatible" ? "danger" : "warning"}
              label={t("panel.status.model", { status: modelStatusLabel })}
            />
            {task ? (
              <StatusBadge
                tone={taskStatusTone(taskStatus)}
                label={Number(task.max_steps) > 0
                  ? t("panel.status.task", { step: task.step, max: task.max_steps })
                  : t("panel.status.taskUnlimited", { step: task.step })}
              />
            ) : null}
          </ToolbarGroup>
        </Toolbar>

        <SegmentedControl
          value={tab}
          options={[
            { value: "overview", label: t("panel.tabs.overview") },
            { value: "model", label: t("panel.tabs.model") },
            { value: "tasks", label: t("panel.tabs.tasks") },
          ]}
          onChange={(value) => { setTab(String(value)) }}
        />

        {task ? (
          <Card title={t("panel.live.title")}>
            <Toolbar>
              <ToolbarGroup>
                <StatusBadge
                  tone={taskStatusTone(taskStatus)}
                  label={taskStatusLabel(taskStatus)}
                />
                <StatusBadge tone="info" label={phaseLabel || t("panel.live.running")} />
                <Text>{t("panel.live.steps", { step: task.step })}</Text>
                <Text>
                  {t("panel.live.elapsed", {
                    elapsed: formatDuration(Number(task.elapsed_seconds) || 0),
                  })}
                </Text>
              </ToolbarGroup>
            </Toolbar>
            <Progress
              indeterminate
              label={task.phase === "thinking" ? t("panel.live.thinking") : undefined}
            />
            <Text>{String(task.message || "")}</Text>
            {task.last_tool ? (
              <Text>
                {t("panel.live.lastTool")}: {String(task.last_tool)}
                {task.last_tool_detail ? ` · ${String(task.last_tool_detail)}` : ""}
              </Text>
            ) : null}
            <Toolbar>
              <ToolbarGroup>
                <Button onClick={() => { setTab("tasks") }}>
                  {t("panel.live.detail")}
                </Button>
                <Button
                  tone="danger"
                  onClick={() => call("cancel_task", { task_id: task.task_id })}
                >
                  {t("panel.activity.cancel")}
                </Button>
              </ToolbarGroup>
            </Toolbar>
          </Card>
        ) : null}

        {tab === "overview" ? (
          <Stack>
            <Card title={t("panel.connection.title")}>
              <Toolbar>
                <ToolbarGroup>
                  <Button onClick={() => call("start_server")}>{t("panel.connection.start")}</Button>
                  <Button onClick={() => call("stop_server")}>{t("panel.connection.stop")}</Button>
                  <Button onClick={() => call("regenerate_token")}>
                    {t("panel.connection.regenerate")}
                  </Button>
                </ToolbarGroup>
              </Toolbar>
              <KeyValue
                items={[
                  {
                    key: "server",
                    label: t("panel.connection.server"),
                    value: server.running ? t("panel.connection.running") : t("panel.connection.stopped"),
                  },
                  { key: "port", label: t("panel.connection.port"), value: server.port || "—" },
                  { key: "discovery", label: t("panel.connection.discovery"), value: discoveryPath },
                  { key: "token", label: t("panel.connection.token"), value: tokenPreview },
                ]}
              />
              {server.error ? <Alert tone="danger" message={String(server.error)} /> : null}
            </Card>

            <Card title={t("panel.workspace.title")}>
              <DataTable
                data={workspaces as PanelState[]}
                rowKey="workspace_id"
                columns={workspaceColumns}
                emptyText={t("panel.connection.noWorkspaces")}
              />
            </Card>
          </Stack>
        ) : null}

        {tab === "model" ? (
          <Stack>
            <Card title={t("panel.model.title")}>
              <Grid cols={3}>
                <StatCard label={t("panel.model.binding")} value={modelStatusLabel} />
                <StatCard label={t("panel.model.model")} value={String(model.model || "—")} />
                <StatCard
                  label={t("panel.model.ready")}
                  value={modelReady ? t("panel.model.yes") : t("panel.model.no")}
                />
              </Grid>
              <KeyValue
                items={[
                  {
                    key: "slot",
                    label: t("panel.model.slot"),
                    value: String(model.slot_name || model.slot_id || "—"),
                  },
                  {
                    key: "capabilities",
                    label: t("panel.model.capabilities"),
                    value: Array.isArray(model.capabilities) && model.capabilities.length > 0
                      ? model.capabilities.join(", ")
                      : "—",
                  },
                ]}
              />
              {model.error ? <Alert tone="warning" message={`${t("panel.model.note")}: ${String(model.error)}`} /> : null}
              <Toolbar>
                <ToolbarGroup>
                  <Button onClick={testModel}>{t("panel.model.test")}</Button>
                </ToolbarGroup>
              </Toolbar>
              <Text>{t("panel.model.bindHint")}</Text>
              <CodeBlock>{modelLink}</CodeBlock>
              <Divider />
              <Text>{t("panel.model.mode")}</Text>
              <SegmentedControl
                value={modelMode}
                options={[
                  { value: "auto", label: t("panel.model.auto") },
                  { value: "dedicated", label: t("panel.model.dedicated") },
                  { value: "main", label: t("panel.model.main") },
                ]}
                onChange={(value) => { setModelMode(String(value)) }}
              />
              <Text>{t("panel.model.privacy")}</Text>
              <Divider />
              <Text>{t("panel.takeover.title")}</Text>
              <SegmentedControl
                value={takeoverMode}
                options={[
                  { value: "off", label: t("panel.takeover.off") },
                  { value: "suggest", label: t("panel.takeover.suggest") },
                  { value: "auto", label: t("panel.takeover.auto") },
                ]}
                onChange={(value) => { setTakeoverMode(String(value)) }}
              />
              <Text>{t("panel.takeover.hint")}</Text>
            </Card>

            <Card title={t("panel.permission.title")}>
              <Text>{t("panel.permission.hint")}</Text>
              <SegmentedControl
                value={permissionMode}
                options={[
                  { value: "ask", label: t("panel.permission.ask") },
                  { value: "auto_edit", label: t("panel.permission.auto_edit") },
                  { value: "full_access", label: t("panel.permission.full_access") },
                ]}
                onChange={(value) => { setPermissionMode(String(value)) }}
              />
              <Alert tone={permissionTone} message={permissionDescription} />
              <Divider />
              <Switch
                checked={allowTerminal}
                label={t("panel.permission.terminalMaster")}
                onChange={(value) => { setAllowTerminal(Boolean(value)) }}
              />
              <Text>{t("panel.permission.terminalHint")}</Text>
              <Toolbar>
                <ToolbarGroup>
                  <Button tone="primary" onClick={saveSettings}>
                    {t("panel.permission.save")}
                  </Button>
                </ToolbarGroup>
              </Toolbar>
              <Text>{t("panel.behavior.configHint")}</Text>
            </Card>

            <Card title={t("panel.budget.title")}>
              <Text>{t("panel.budget.hint")}</Text>
              <Field label={t("panel.budget.maxSteps")} help={t("panel.budget.maxStepsHint")}>
                <NumberInput
                  value={maxSteps}
                  min={0}
                  step={1}
                  onChange={(value) => { setMaxSteps(Number(value) || 0) }}
                />
              </Field>
              <Field label={t("panel.budget.taskTimeout")} help={t("panel.budget.taskTimeoutHint")}>
                <NumberInput
                  value={taskTimeoutMinutes}
                  min={0}
                  step={1}
                  onChange={(value) => { setTaskTimeoutMinutes(Number(value) || 0) }}
                />
              </Field>
              <Field label={t("panel.budget.modelTimeout")} help={t("panel.budget.modelTimeoutHint")}>
                <NumberInput
                  value={modelTimeoutSeconds}
                  min={0}
                  step={1}
                  onChange={(value) => { setModelTimeoutSeconds(Number(value) || 0) }}
                />
              </Field>
              <Toolbar>
                <ToolbarGroup>
                  <Button tone="primary" onClick={saveSettings}>
                    {t("panel.permission.save")}
                  </Button>
                </ToolbarGroup>
              </Toolbar>
            </Card>
          </Stack>
        ) : null}

        {tab === "tasks" ? (
          <Stack>
            <Card title={t("panel.direct.title")}>
              <Text>{t("panel.direct.hint")}</Text>
              <Textarea
                value={directRequest}
                placeholder={t("panel.direct.placeholder")}
                onChange={(value) => { setDirectRequest(String(value)) }}
              />
              <Toolbar>
                <ToolbarGroup>
                  <Button tone="primary" onClick={startDirectTask}>
                    {t("panel.direct.start")}
                  </Button>
                </ToolbarGroup>
              </Toolbar>
            </Card>

            <Card title={t("panel.task.current")}>
              {task ? (
                <Stack>
                  <Toolbar>
                    <ToolbarGroup>
                      <StatusBadge
                        tone={taskStatusTone(taskStatus)}
                        label={taskStatusLabel(taskStatus)}
                      />
                      <Text>{String(task.task_id)}</Text>
                    </ToolbarGroup>
                  </Toolbar>
                  <Progress
                    indeterminate={Number(task.max_steps) <= 0}
                    value={Number(task.max_steps) > 0 ? taskProgress : undefined}
                    label={t("panel.task.stepsElapsed", {
                      step: task.step,
                      elapsed: formatDuration(Number(task.elapsed_seconds) || 0),
                    })}
                  />
                  <Text>{t("panel.live.phase")}: {phaseLabel || "—"}</Text>
                  <Text>{String(task.message || "")}</Text>
                  {task.last_tool ? (
                    <Text>
                      {t("panel.live.lastTool")}: {String(task.last_tool)}
                      {task.last_tool_detail ? ` · ${String(task.last_tool_detail)}` : ""}
                    </Text>
                  ) : null}
                  {Array.isArray(task.files_changed) && task.files_changed.length > 0 ? (
                    <Text>
                      {t("panel.activity.files")}: {task.files_changed.join(", ")}
                    </Text>
                  ) : null}
                  <Toolbar>
                    <ToolbarGroup>
                      <Button
                        tone="danger"
                        onClick={() => call("cancel_task", { task_id: task.task_id })}
                      >
                        {t("panel.activity.cancel")}
                      </Button>
                    </ToolbarGroup>
                  </Toolbar>
                </Stack>
              ) : (
                <EmptyState
                  title={t("panel.activity.noActive")}
                  description={t("panel.task.emptyHint")}
                />
              )}
            </Card>

            <Card title={t("panel.task.activity")}>
              <DataTable
                data={activity as PanelState[]}
                rowKey="id"
                columns={activityColumns}
                emptyText={t("panel.task.emptyActivity")}
              />
            </Card>

            <Card title={t("panel.task.history")}>
              <DataTable
                data={history as PanelState[]}
                rowKey="task_id"
                columns={historyColumns}
                emptyText={t("panel.activity.noHistory")}
              />
              <Toolbar>
                <ToolbarGroup>
                  <Button onClick={() => call("clear_history")}>{t("panel.activity.clear")}</Button>
                </ToolbarGroup>
              </Toolbar>
            </Card>
          </Stack>
        ) : null}
      </Stack>
    </Page>
  )
}
