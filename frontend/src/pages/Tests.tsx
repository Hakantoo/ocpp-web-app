/** Stress testing: build a short, ordered sequence of steps against real,
 *  genuinely simulated hardware, then run it.
 *
 *  Every "create" step gets a run-specific identity prefix, so a later
 *  "delete" step can find and remove exactly what a run created -- real
 *  chargers, real cards, real vehicles, all genuinely deleted, not the
 *  history-preserving delete a real charger gets. This runs server-side:
 *  the whole step sequence is sent once, and the browser just watches
 *  progress rather than looping through potentially thousands of calls
 *  itself.
 */

import { useMemo, useState } from "react";
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronRight,
  FlaskConical,
  Loader2,
  Pencil,
  Play,
  Plus,
  Save,
  Trash2,
  X,
  Zap,
} from "lucide-react";

import {
  useCancelStressTest,
  useDeleteTestDefinition,
  useUpdateTestDefinition,
  useRunTestDefinition,
  useSaveTestDefinition,
  useStartStressTest,
  useStressTestList,
  useTestDefinitions,
  type StressStep,
  type StressStepKind,
  type TestDefinition,
} from "../lib/api";
import { sortByFavorite, useFavorites } from "../lib/favorites";
import {
  Button,
  ChargerSearch,
  ErrorNote,
  FavoriteStar,
  Field,
  Input,
  Panel,
  PanelHeader,
  Select,
} from "../components/ui";

const STEP_LABELS: Record<StressStepKind, string> = {
  create: "Create chargers",
  plug_in: "Plug in",
  unplug: "Unplug",
  present_card: "Present card",
  charge: "Offer power",
  stop_charge: "Withdraw power",
  remote_start: "Remote start",
  remote_stop: "Remote stop",
  fault: "Inject fault",
  clear_fault: "Clear fault",
  wait: "Wait",
  delete: "Delete chargers",
};

function blankStep(kind: StressStepKind): StressStep {
  switch (kind) {
    case "create":
      return { kind, count: 10, connectors: 1 };
    case "wait":
      return { kind, seconds: 1 };
    case "delete":
      return { kind, delete_target: "created_here" };
    default:
      return { kind };
  }
}

export function Tests() {
  const [name, setName] = useState("Test");
  const [steps, setSteps] = useState<StressStep[]>([blankStep("create")]);
  const [savedSearch, setSavedSearch] = useState("");
  const [historySearch, setHistorySearch] = useState("");

  const start = useStartStressTest();
  const cancel = useCancelStressTest();
  const { data: runs } = useStressTestList();

  const { data: saved } = useTestDefinitions();
  const save = useSaveTestDefinition();
  const runSaved = useRunTestDefinition();
  const updateSaved = useUpdateTestDefinition();
  const deleteSaved = useDeleteTestDefinition();
  const [editingId, setEditingId] = useState<number | null>(null);
  const [expandedRuns, setExpandedRuns] = useState<Set<string>>(new Set());
  const { isFavorite, toggleFavorite } = useFavorites();

  function toggleRun(id: string) {
    setExpandedRuns((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const visibleSaved = useMemo(() => {
    if (!saved) return [];
    const q = savedSearch.trim().toLowerCase();
    const filtered = q ? saved.filter((t) => t.name.toLowerCase().includes(q)) : saved;
    return sortByFavorite(filtered, (t) => String(t.id), isFavorite);
  }, [saved, savedSearch, isFavorite]);

  const visibleRuns = useMemo(() => {
    if (!runs) return [];
    const q = historySearch.trim().toLowerCase();
    const list = q ? runs.filter((r) => r.name.toLowerCase().includes(q)) : runs;
    return [...list].reverse();
  }, [runs, historySearch]);

  function addStep() {
    setSteps((prev) => [...prev, blankStep("create")]);
  }

  function removeStep(index: number) {
    setSteps((prev) => prev.filter((_, i) => i !== index));
  }

  function updateStep(index: number, patch: Partial<StressStep>) {
    setSteps((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function changeKind(index: number, kind: StressStepKind) {
    setSteps((prev) => prev.map((s, i) => (i === index ? blankStep(kind) : s)));
  }

  async function execute() {
    await start.mutateAsync({ name, steps });
    setName("Test");
    setSteps([blankStep("create")]);
  }

  async function saveDefinition() {
    await save.mutateAsync({ name, steps });
  }

  const totalChargersToCreate = steps
    .filter((s) => s.kind === "create")
    .reduce((sum, s) => sum + (s.count ?? 0), 0);

  return (
    <div className="space-y-5">
      <div>
        <p className="eyebrow mb-1.5">Load</p>
        <h1 className="text-2xl font-semibold tracking-tight">Tests</h1>
        <p className="mt-2 max-w-2xl text-xs text-ink-faint">
          Build a short, ordered sequence of steps and run it against real,
          genuinely simulated chargers -- actual WebSocket connections, real
          OCPP. Steps run one at a time, in order. Nothing here touches
          anything you already have; every charger a run creates gets its
          own identity prefix, and a Delete step is the only thing that
          removes it.
        </p>
      </div>

      <Panel className="space-y-4 p-4">
        <Field label="Test name">
          <Input value={name} onChange={setName} placeholder="Test 1" />
        </Field>

        <div className="space-y-3">
          {steps.map((step, i) => (
            <StepRow
              key={i}
              index={i}
              step={step}
              onKindChange={(k) => changeKind(i, k)}
              onChange={(patch) => updateStep(i, patch)}
              onRemove={() => removeStep(i)}
              canRemove={steps.length > 1}
            />
          ))}
        </div>

        <div className="flex items-center justify-between border-t border-line pt-3">
          <Button onClick={addStep}>
            <Plus size={13} /> Add step
          </Button>
          <div className="flex items-center gap-3">
            {totalChargersToCreate > 100 && (
              <span className="flex items-center gap-1 text-xs text-signal-hold">
                <AlertTriangle size={12} /> {totalChargersToCreate} chargers total
              </span>
            )}
            <Button busy={save.isPending} disabled={steps.length === 0} onClick={saveDefinition}>
              <Save size={13} /> Save
            </Button>
            <Button
              variant="primary"
              busy={start.isPending}
              disabled={steps.length === 0}
              onClick={execute}
            >
              <Zap size={13} /> Execute
            </Button>
          </div>
        </div>

        {start.error && (
          <ErrorNote message={start.error instanceof Error ? start.error.message : "Could not start"} />
        )}
        {save.error && (
          <ErrorNote message={save.error instanceof Error ? save.error.message : "Could not save"} />
        )}
      </Panel>

      {saved && saved.length > 0 && (
        <Panel>
          <PanelHeader eyebrow="Saved" title="Reusable tests" />
          <div className="space-y-2 p-4">
            {saved.length > 3 && (
              <ChargerSearch
                value={savedSearch}
                onChange={setSavedSearch}
                placeholder="Search saved tests"
              />
            )}
            {visibleSaved.length === 0 ? (
              <p className="text-xs text-ink-faint">No saved tests match.</p>
            ) : (
              visibleSaved.map((t) =>
                editingId === t.id ? (
                  <SavedTestEditor
                    key={t.id}
                    definition={t}
                    onCancel={() => setEditingId(null)}
                    onSave={async (name, steps) => {
                      await updateSaved.mutateAsync({ id: t.id, name, steps });
                      setEditingId(null);
                    }}
                    busy={updateSaved.isPending}
                  />
                ) : (
                  <div
                    key={t.id}
                    className="flex items-center gap-3 rounded-lg border border-line px-3 py-2"
                  >
                    <FavoriteStar
                      active={isFavorite(String(t.id))}
                      onToggle={() => toggleFavorite(String(t.id))}
                      label={t.name}
                    />
                    <FlaskConical size={14} className="shrink-0 text-ink-faint" />
                    <span className="flex-1 truncate text-sm text-ink">
                      {t.name}{" "}
                      <span className="text-xs text-ink-faint">
                        · {t.steps.length} step{t.steps.length === 1 ? "" : "s"}
                      </span>
                    </span>
                    <Button
                      busy={runSaved.isPending}
                      onClick={() => runSaved.mutateAsync(t.id)}
                    >
                      <Play size={13} /> Run
                    </Button>
                    <button
                      type="button"
                      onClick={() => setEditingId(t.id)}
                      className="rounded-md p-1 text-ink-faint hover:text-ink"
                      aria-label={`Edit ${t.name}`}
                    >
                      <Pencil size={13} />
                    </button>
                    <button
                      type="button"
                      onClick={() => deleteSaved.mutateAsync(t.id)}
                      className="rounded-md p-1 text-ink-faint hover:text-signal-fault"
                      aria-label={`Delete ${t.name}`}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ),
              )
            )}
          </div>
        </Panel>
      )}

      {runs && runs.length > 0 && (
        <div className="space-y-3">
          {runs.length > 3 && (
            <ChargerSearch
              value={historySearch}
              onChange={setHistorySearch}
              placeholder="Search run history"
            />
          )}
          {visibleRuns.map((run) => {
            const isOpen = expandedRuns.has(run.id) || run.status === "running";
            return (
              <Panel key={run.id}>
                <button
                  type="button"
                  onClick={() => toggleRun(run.id)}
                  className="flex w-full items-center justify-between gap-4 border-b border-line px-4 py-3 text-left hover:bg-panel-high/40"
                >
                  <div className="flex min-w-0 items-center gap-2">
                    {isOpen ? (
                      <ChevronDown size={14} className="shrink-0 text-ink-faint" />
                    ) : (
                      <ChevronRight size={14} className="shrink-0 text-ink-faint" />
                    )}
                    <div className="min-w-0">
                      <p className="eyebrow mb-0.5">{run.name}</p>
                      <h2 className="truncate text-sm font-semibold text-ink">
                        {run.status === "running"
                          ? "Running…"
                          : run.status === "done"
                            ? "Done"
                            : run.status === "cancelled"
                              ? "Cancelled"
                              : "Failed"}
                        {run.steps.length > 0 && (
                          <span className="tnum ml-2 text-xs font-normal text-ink-faint">
                            {new Date(run.steps[0].started_at * 1000).toLocaleString([], {
                              month: "short",
                              day: "numeric",
                              hour: "2-digit",
                              minute: "2-digit",
                            })}
                          </span>
                        )}
                      </h2>
                    </div>
                  </div>
                  {run.status === "running" ? (
                    <div onClick={(e) => e.stopPropagation()}>
                      <Button onClick={() => cancel.mutateAsync(run.id)} busy={cancel.isPending}>
                        <X size={13} /> Cancel
                      </Button>
                    </div>
                  ) : null}
                </button>
                {isOpen && (
                  <div className="space-y-2 p-4">
                    {run.steps.map((r, i) => (
                      <div
                        key={i}
                        className="flex items-center gap-3 rounded-lg border border-line px-3 py-2 text-sm"
                      >
                        {r.finished_at === null ? (
                          <Loader2 size={14} className="shrink-0 animate-spin text-ink-faint" />
                        ) : r.ok ? (
                          <Check size={14} className="shrink-0 text-signal-live" />
                        ) : (
                          <X size={14} className="shrink-0 text-signal-fault" />
                        )}
                        <span className="w-36 shrink-0 text-ink">{STEP_LABELS[r.kind]}</span>
                        <span className="min-w-0 flex-1 truncate text-xs text-ink-faint">
                          {r.detail}
                        </span>
                        <span className="tnum shrink-0 text-xs text-ink-faint">
                          {new Date(r.started_at * 1000).toLocaleTimeString([], {
                            hour: "2-digit",
                            minute: "2-digit",
                            second: "2-digit",
                          })}
                        </span>
                      </div>
                    ))}
                    {run.total_steps > run.steps.length && (
                      <p className="text-xs text-ink-faint">
                        {run.total_steps - run.steps.length} step
                        {run.total_steps - run.steps.length === 1 ? "" : "s"} still queued
                      </p>
                    )}
                  </div>
                )}
              </Panel>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** Editing a saved test in place: the exact same step-builder rows as the
 *  main form above, just pre-filled with what was already saved, and
 *  replacing that one row in the list rather than opening elsewhere. */
function SavedTestEditor({
  definition,
  onCancel,
  onSave,
  busy,
}: {
  definition: TestDefinition;
  onCancel: () => void;
  onSave: (name: string, steps: StressStep[]) => void;
  busy: boolean;
}) {
  const [name, setName] = useState(definition.name);
  const [steps, setSteps] = useState<StressStep[]>(definition.steps);

  function addStep() {
    setSteps((prev) => [...prev, blankStep("create")]);
  }

  function removeStep(index: number) {
    setSteps((prev) => prev.filter((_, i) => i !== index));
  }

  function updateStep(index: number, patch: Partial<StressStep>) {
    setSteps((prev) => prev.map((s, i) => (i === index ? { ...s, ...patch } : s)));
  }

  function changeKind(index: number, kind: StressStepKind) {
    setSteps((prev) => prev.map((s, i) => (i === index ? blankStep(kind) : s)));
  }

  return (
    <div className="space-y-3 rounded-lg border border-signal-wait/40 bg-panel-high/20 p-3">
      <Field label="Test name">
        <Input value={name} onChange={setName} />
      </Field>
      {steps.map((step, i) => (
        <StepRow
          key={i}
          index={i}
          step={step}
          onKindChange={(k) => changeKind(i, k)}
          onChange={(patch) => updateStep(i, patch)}
          onRemove={() => removeStep(i)}
          canRemove={steps.length > 1}
        />
      ))}
      <div className="flex items-center justify-between">
        <Button onClick={addStep}>
          <Plus size={13} /> Add step
        </Button>
        <div className="flex gap-2">
          <Button onClick={onCancel}>Cancel</Button>
          <Button
            variant="primary"
            busy={busy}
            disabled={!name.trim() || steps.length === 0}
            onClick={() => onSave(name.trim(), steps)}
          >
            <Save size={13} /> Save changes
          </Button>
        </div>
      </div>
    </div>
  );
}

function StepRow({
  index,
  step,
  onKindChange,
  onChange,
  onRemove,
  canRemove,
}: {
  index: number;
  step: StressStep;
  onKindChange: (kind: StressStepKind) => void;
  onChange: (patch: Partial<StressStep>) => void;
  onRemove: () => void;
  canRemove: boolean;
}) {
  return (
    <div className="rounded-lg border border-line p-3">
      <div className="mb-2 flex items-center gap-2">
        <span className="tnum text-xs text-ink-faint">Step {index + 1}</span>
        <div className="w-44">
          <Select
            value={step.kind}
            onChange={(v) => onKindChange(v as StressStepKind)}
            options={Object.entries(STEP_LABELS).map(([value, label]) => ({
              value,
              label,
            }))}
          />
        </div>
        {canRemove && (
          <button
            type="button"
            onClick={onRemove}
            className="ml-auto rounded-md p-1 text-ink-faint hover:text-signal-fault"
            aria-label="Remove step"
          >
            <Trash2 size={13} />
          </button>
        )}
      </div>

      {step.kind === "create" && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          <Field label="How many chargers">
            <Input
              type="number"
              min={1}
              max={20000}
              value={String(step.count ?? 1)}
              onChange={(v) => onChange({ count: Number(v) })}
            />
          </Field>
          <Field label="Connectors each">
            <Select
              value={String(step.connectors ?? 1)}
              onChange={(v) => onChange({ connectors: Number(v) })}
              options={[1, 2, 3, 4, 5, 6, 7, 8].map((n) => ({
                value: String(n),
                label: String(n),
              }))}
            />
          </Field>
        </div>
      )}

      {(step.kind === "wait") && (
        <Field label="Seconds">
          <Input
            type="number"
            min={0}
            step={0.5}
            value={String(step.seconds ?? 1)}
            onChange={(v) => onChange({ seconds: Number(v) })}
          />
        </Field>
      )}

      {step.kind === "delete" && (
        <p className="text-xs text-ink-faint">
          Fully removes every charger, card, and vehicle this test has
          created so far -- a genuine delete, not the history-preserving one
          a real charger gets. Nothing this test never created is touched.
        </p>
      )}

      {(step.kind === "fault" || step.kind === "clear_fault") && (
        <p className="text-xs text-ink-faint">
          Applies to every charger this test has created so far.
        </p>
      )}
    </div>
  );
}