/** Cards and cars.
 *
 *  Two independent lists. A card is a credential: a number, whether it works,
 *  and when it stops working. A car is a battery with a name. Neither knows
 *  about the other, because the only place both facts are true together is a
 *  session -- which card authorised it, and which car was connected.
 */

import { useEffect, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";
import { Pencil, Plus, Trash2 } from "lucide-react";

import {
  useCreateTag,
  useCreateVehicle,
  useDeleteTag,
  useDeleteVehicle,
  useTags,
  useUpdateTag,
  useUpdateVehicle,
  useVehicles,
} from "../lib/api";
import { datetime, pct } from "../lib/format";
import type { IdTag, Vehicle } from "../lib/types";
import {
  Button,
  Chip,
  EmptyState,
  ErrorNote,
  Field,
  Input,
  Modal,
  Panel,
  PanelHeader,
  Select,
  Skeleton,
  Table,
  Td,
  Th,
} from "../components/ui";

const TAG_SIGNAL: Record<IdTag["status"], "live" | "fault" | "hold" | "idle"> = {
  Accepted: "live",
  Blocked: "fault",
  Expired: "hold",
  Invalid: "fault",
  ConcurrentTx: "hold",
};

const TAG_STATUSES = ["Accepted", "Blocked", "Expired", "Invalid"].map((s) => ({
  value: s,
  label: s,
}));

type Editing =
  | { kind: "tag"; tag: IdTag | null; prefillIdTag?: string }
  | { kind: "vehicle"; vehicle: Vehicle | null }
  | null;

export function Directory() {
  const tags = useTags();
  const vehicles = useVehicles();

  const [editing, setEditing] = useState<Editing>(null);
  const [failure, setFailure] = useState<string | null>(null);

  // Arriving from the unknown-card prompt: open the add form with the number
  // already filled in, so the operator only has to choose whether to accept it.
  const location = useLocation();
  const navigate = useNavigate();
  const arrivedWith = (location.state as { addCard?: string } | null)?.addCard;
  useEffect(() => {
    if (!arrivedWith) return;
    setEditing({ kind: "tag", tag: null, prefillIdTag: arrivedWith });
    // Clear it so a refresh or a back-navigation does not reopen the form.
    navigate(location.pathname, { replace: true, state: null });
  }, [arrivedWith, navigate, location.pathname]);

  const deleteTag = useDeleteTag();
  const deleteVehicle = useDeleteVehicle();

  if (tags.isLoading || vehicles.isLoading) return <Skeleton className="h-72" />;
  if (tags.error) return <ErrorNote message={(tags.error as Error).message} />;

  async function remove(run: () => Promise<unknown>) {
    setFailure(null);
    try {
      await run();
    } catch (err) {
      setFailure(err instanceof Error ? err.message : "Could not delete that");
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <p className="eyebrow mb-1.5">Directory</p>
        <h1 className="text-2xl font-semibold tracking-tight">Cards &amp; cars</h1>
      </div>

      {failure && <ErrorNote message={failure} />}

      {/* Cards */}
      <Panel>
        <PanelHeader
          eyebrow="Authorisation"
          title="RFID cards"
          right={
            <Button
              variant="primary"
              onClick={() => setEditing({ kind: "tag", tag: null })}
            >
              <Plus size={13} /> Add card
            </Button>
          }
        />
        {!tags.data?.length ? (
          <EmptyState
            title="No cards yet"
            hint="A card is what authorises a session to start."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Card</Th>
                <Th>Status</Th>
                <Th>Expires</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {tags.data.map((tag) => (
                <tr key={tag.id_tag}>
                  <Td className="tnum text-ink">{tag.id_tag}</Td>
                  <Td>
                    <Chip signal={TAG_SIGNAL[tag.status]}>{tag.status}</Chip>
                  </Td>
                  <Td className="tnum">
                    {tag.expiry_date ? datetime(tag.expiry_date) : "Never"}
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-1.5">
                      <Button onClick={() => setEditing({ kind: "tag", tag })}>
                        <Pencil size={12} />
                      </Button>
                      <Button
                        variant="danger"
                        onClick={() => remove(() => deleteTag.mutateAsync(tag.id_tag))}
                      >
                        <Trash2 size={12} />
                      </Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>

      {/* Cars */}
      <Panel>
        <PanelHeader
          eyebrow="Fleet"
          title="Vehicles"
          right={
            <Button
              variant="primary"
              onClick={() => setEditing({ kind: "vehicle", vehicle: null })}
            >
              <Plus size={13} /> Add vehicle
            </Button>
          }
        />
        {!vehicles.data?.length ? (
          <EmptyState
            title="No vehicles yet"
            hint="Add one and it becomes pluggable in the simulator straight away."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Vehicle</Th>
                <Th className="text-right">Battery</Th>
                <Th className="text-right">Max rate</Th>
                <Th className="w-44">Charge</Th>
                <Th>Plugged in</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {vehicles.data.map((v) => (
                <tr key={v.id}>
                  <Td className="text-ink">{v.name}</Td>
                  <Td className="tnum text-right">{v.battery_capacity_kwh} kWh</Td>
                  <Td className="tnum text-right">{v.max_charge_kw} kW</Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-panel-high">
                        <div
                          className="h-full rounded-full bg-signal-live transition-all"
                          style={{ width: `${Math.min(100, v.current_soc)}%` }}
                        />
                      </div>
                      <span className="tnum w-10 text-right text-xs text-ink">
                        {pct(v.current_soc, 1)}
                      </span>
                    </div>
                  </Td>
                  <Td className="tnum">
                    {v.charge_point_id
                      ? `${v.charge_point_id}·${v.connector_id}`
                      : "—"}
                  </Td>
                  <Td className="text-right">
                    <div className="flex justify-end gap-1.5">
                      <Button onClick={() => setEditing({ kind: "vehicle", vehicle: v })}>
                        <Pencil size={12} />
                      </Button>
                      <Button
                        variant="danger"
                        onClick={() => remove(() => deleteVehicle.mutateAsync(v.id))}
                      >
                        <Trash2 size={12} />
                      </Button>
                    </div>
                  </Td>
                </tr>
              ))}
            </tbody>
          </Table>
        )}
      </Panel>

      {editing?.kind === "tag" && (
        <TagEditor
          tag={editing.tag}
          prefillIdTag={editing.prefillIdTag}
          onClose={() => setEditing(null)}
        />
      )}
      {editing?.kind === "vehicle" && (
        <VehicleEditor vehicle={editing.vehicle} onClose={() => setEditing(null)} />
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------

function TagEditor({
  tag,
  prefillIdTag,
  onClose,
}: {
  tag: IdTag | null;
  prefillIdTag?: string;
  onClose: () => void;
}) {
  const create = useCreateTag();
  const update = useUpdateTag();
  const [idTag, setIdTag] = useState(tag?.id_tag ?? prefillIdTag ?? "");
  const [status, setStatus] = useState<IdTag["status"]>(tag?.status ?? "Accepted");
  const [expiry, setExpiry] = useState(tag?.expiry_date?.slice(0, 16) ?? "");
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setError(null);
    const changes = {
      status,
      // datetime-local has no timezone; the API stores ISO-8601 UTC.
      expiry_date: expiry ? new Date(expiry).toISOString() : null,
    };
    try {
      if (tag) await update.mutateAsync({ id_tag: tag.id_tag, changes });
      else await create.mutateAsync({ id_tag: idTag, ...changes });
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    }
  }

  return (
    <Modal
      title={tag ? `Edit ${tag.id_tag}` : "Add a card"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            onClick={save}
            busy={create.isPending || update.isPending}
          >
            {tag ? "Save changes" : "Add card"}
          </Button>
        </>
      }
    >
      {error && <ErrorNote message={error} />}
      {!tag && (
        <Field label="Card number" hint="Up to 20 characters, as printed on the card">
          <Input value={idTag} onChange={setIdTag} placeholder="RFID-0003" />
        </Field>
      )}
      <Field label="Status">
        <Select
          value={status}
          onChange={(v) => setStatus(v as IdTag["status"])}
          options={TAG_STATUSES}
        />
      </Field>
      <Field label="Expires" hint="Leave empty for a card that never expires">
        <Input type="datetime-local" value={expiry} onChange={setExpiry} />
      </Field>
    </Modal>
  );
}

function VehicleEditor({
  vehicle,
  onClose,
}: {
  vehicle: Vehicle | null;
  onClose: () => void;
}) {
  const create = useCreateVehicle();
  const update = useUpdateVehicle();
  const [name, setName] = useState(vehicle?.name ?? "");
  const [capacity, setCapacity] = useState(String(vehicle?.battery_capacity_kwh ?? 64));
  const [maxKw, setMaxKw] = useState(String(vehicle?.max_charge_kw ?? 11));
  const [soc, setSoc] = useState(String(vehicle?.current_soc ?? 20));
  const [error, setError] = useState<string | null>(null);

  async function save() {
    setError(null);
    const body = {
      name,
      battery_capacity_kwh: Number(capacity),
      max_charge_kw: Number(maxKw),
      current_soc: Number(soc),
    };
    try {
      if (vehicle) await update.mutateAsync({ id: vehicle.id, changes: body });
      else await create.mutateAsync(body);
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save that");
    }
  }

  return (
    <Modal
      title={vehicle ? `Edit ${vehicle.name}` : "Add a vehicle"}
      onClose={onClose}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            onClick={save}
            busy={create.isPending || update.isPending}
          >
            {vehicle ? "Save changes" : "Add vehicle"}
          </Button>
        </>
      }
    >
      {error && <ErrorNote message={error} />}
      <Field label="Name">
        <Input value={name} onChange={setName} placeholder="Kona Electric" />
      </Field>
      <div className="grid grid-cols-2 gap-3">
        <Field label="Battery (kWh)">
          <Input
            type="number"
            min={1}
            step={0.1}
            value={capacity}
            onChange={setCapacity}
          />
        </Field>
        <Field label="Max rate (kW)">
          <Input type="number" min={1} step={0.1} value={maxKw} onChange={setMaxKw} />
        </Field>
      </div>
      <Field label="Charge now (%)" hint="Charging runs until the battery is full">
        <Input type="number" min={0} max={100} step={1} value={soc} onChange={setSoc} />
      </Field>
    </Modal>
  );
}