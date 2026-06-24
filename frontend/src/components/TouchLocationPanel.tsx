import { useEffect, useState } from "react";

import type { TouchLocation, TouchMap } from "../types/domain";

type TouchLocationPanelProps = {
  locations: TouchLocation[];
  maps: TouchMap[];
  canManage: boolean;
  onCreateLocation: (payload: {
    name: string;
    category: string | null;
    description: string | null;
    floor_zone: string | null;
    suite: string | null;
    image_url: string | null;
    is_active: boolean;
  }) => void;
  onUpdateLocation: (
    locationId: string,
    payload: {
      name: string;
      category: string | null;
      description: string | null;
      floor_zone: string | null;
      suite: string | null;
      image_url: string | null;
      is_active: boolean;
    },
  ) => void;
  onDeleteLocation: (locationId: string) => void;
  onCreateMap: (payload: {
    name: string;
    floor_zone: string | null;
    background_url: string | null;
    overlay_url: string | null;
    is_active: boolean;
  }) => void;
  onUpdateMap: (
    mapId: string,
    payload: {
      name: string;
      floor_zone: string | null;
      background_url: string | null;
      overlay_url: string | null;
      is_active: boolean;
    },
  ) => void;
  onDeleteMap: (mapId: string) => void;
};

export function TouchLocationPanel({
  locations,
  maps,
  canManage,
  onCreateLocation,
  onUpdateLocation,
  onDeleteLocation,
  onCreateMap,
  onUpdateMap,
  onDeleteMap,
}: TouchLocationPanelProps) {
  const [selectedLocationId, setSelectedLocationId] = useState<string | null>(null);
  const [selectedMapId, setSelectedMapId] = useState<string | null>(null);
  const [locationForm, setLocationForm] = useState({
    name: "",
    category: "",
    description: "",
    floor_zone: "",
    suite: "",
    image_url: "",
    is_active: true,
  });
  const [mapForm, setMapForm] = useState({
    name: "",
    floor_zone: "",
    background_url: "",
    overlay_url: "",
    is_active: true,
  });

  const selectedLocation = locations.find((item) => item.id === selectedLocationId) ?? null;
  const selectedMap = maps.find((item) => item.id === selectedMapId) ?? null;

  useEffect(() => {
    if (!selectedLocation) {
      return;
    }

    setLocationForm({
      name: selectedLocation.name,
      category: selectedLocation.category ?? "",
      description: selectedLocation.description ?? "",
      floor_zone: selectedLocation.floor_zone ?? "",
      suite: selectedLocation.suite ?? "",
      image_url: selectedLocation.image_url ?? "",
      is_active: selectedLocation.is_active,
    });
  }, [selectedLocation]);

  useEffect(() => {
    if (!selectedMap) {
      return;
    }

    setMapForm({
      name: selectedMap.name,
      floor_zone: selectedMap.floor_zone ?? "",
      background_url: selectedMap.background_url ?? "",
      overlay_url: selectedMap.overlay_url ?? "",
      is_active: selectedMap.is_active,
    });
  }, [selectedMap]);

  return (
    <section className="rounded-[32px] border border-white/70 bg-card/90 p-6 shadow-card backdrop-blur">
      <div className="border-b border-slate-200 pb-5">
        <h2 className="font-display text-2xl text-ink">Locations y mapas</h2>
        <p className="mt-1 text-sm text-slate-600">Carga tiendas, categorias, zonas, locales y mapas base para que el futuro player touch reciba un runtime claro.</p>
      </div>

      {!canManage ? (
        <div className="mt-6 rounded-[24px] border border-slate-200 bg-slate-50 px-5 py-6 text-sm text-slate-600">
          Este rol solo puede consultar locations, mapas y hotspots existentes.
        </div>
      ) : (
        <div className="mt-6 grid gap-5 xl:grid-cols-2">
          <div className="space-y-5">
            <div className="space-y-3">
              {locations.length > 0 ? (
                locations.map((location) => (
                  <button
                    key={location.id}
                    type="button"
                    onClick={() => setSelectedLocationId(location.id)}
                    className={[
                      "w-full rounded-[24px] border p-4 text-left transition",
                      selectedLocationId === location.id ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/35",
                    ].join(" ")}
                  >
                    <p className="font-semibold text-ink">{location.name}</p>
                    <p className="mt-2 text-sm text-slate-500">
                      {location.category ?? "Sin categoria"} · {location.floor_zone ?? "Sin zona"} · {location.suite ?? "Sin local"}
                    </p>
                  </button>
                ))
              ) : (
                <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                  Aún no hay locations creadas.
                </div>
              )}
            </div>

            <div className="space-y-4 rounded-[26px] border border-slate-200 bg-slate-50 p-5">
              <p className="font-semibold text-ink">Ficha de location</p>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                  <input value={locationForm.name} onChange={(event) => setLocationForm({ ...locationForm, name: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Categoría</label>
                  <input value={locationForm.category} onChange={(event) => setLocationForm({ ...locationForm, category: event.target.value })} />
                </div>
              </div>
              <div>
                <label className="mb-2 block text-sm font-semibold text-slate-700">Descripción</label>
                <textarea
                  className="min-h-[100px]"
                  value={locationForm.description}
                  onChange={(event) => setLocationForm({ ...locationForm, description: event.target.value })}
                />
              </div>
              <div className="grid gap-4 md:grid-cols-3">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Piso / zona</label>
                  <input value={locationForm.floor_zone} onChange={(event) => setLocationForm({ ...locationForm, floor_zone: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Local</label>
                  <input value={locationForm.suite} onChange={(event) => setLocationForm({ ...locationForm, suite: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Logo / imagen</label>
                  <input value={locationForm.image_url} onChange={(event) => setLocationForm({ ...locationForm, image_url: event.target.value })} />
                </div>
              </div>
              <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-white px-4 py-4">
                <input
                  className="h-5 w-5"
                  type="checkbox"
                  checked={locationForm.is_active}
                  onChange={(event) => setLocationForm({ ...locationForm, is_active: event.target.checked })}
                />
                <span className="text-sm font-semibold text-slate-700">Location activa</span>
              </label>
              <div className="flex flex-wrap gap-3">
                <button
                  className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white"
                  type="button"
                  onClick={() =>
                    onCreateLocation({
                      name: locationForm.name,
                      category: locationForm.category || null,
                      description: locationForm.description || null,
                      floor_zone: locationForm.floor_zone || null,
                      suite: locationForm.suite || null,
                      image_url: locationForm.image_url || null,
                      is_active: locationForm.is_active,
                    })
                  }
                >
                  Crear location
                </button>
                <button
                  className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedLocation}
                  onClick={() => {
                    if (selectedLocation) {
                      onUpdateLocation(selectedLocation.id, {
                        name: locationForm.name,
                        category: locationForm.category || null,
                        description: locationForm.description || null,
                        floor_zone: locationForm.floor_zone || null,
                        suite: locationForm.suite || null,
                        image_url: locationForm.image_url || null,
                        is_active: locationForm.is_active,
                      });
                    }
                  }}
                >
                  Guardar
                </button>
                <button
                  className="rounded-full border border-rose-200 px-5 py-3 text-sm font-semibold text-rose-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedLocation}
                  onClick={() => {
                    if (selectedLocation) {
                      onDeleteLocation(selectedLocation.id);
                    }
                  }}
                >
                  Eliminar
                </button>
              </div>
            </div>
          </div>

          <div className="space-y-5">
            <div className="space-y-3">
              {maps.length > 0 ? (
                maps.map((mapItem) => (
                  <button
                    key={mapItem.id}
                    type="button"
                    onClick={() => setSelectedMapId(mapItem.id)}
                    className={[
                      "w-full rounded-[24px] border p-4 text-left transition",
                      selectedMapId === mapItem.id ? "border-accent bg-accentSoft/35" : "border-slate-200 bg-white hover:border-accent/35",
                    ].join(" ")}
                  >
                    <p className="font-semibold text-ink">{mapItem.name}</p>
                    <p className="mt-2 text-sm text-slate-500">
                      {mapItem.floor_zone ?? "Sin zona"} · {mapItem.background_url ? "Con fondo" : "Sin fondo"}
                    </p>
                  </button>
                ))
              ) : (
                <div className="rounded-[24px] border border-dashed border-slate-300 bg-slate-50 px-5 py-6 text-sm text-slate-600">
                  Aún no hay mapas base cargados.
                </div>
              )}
            </div>

            <div className="space-y-4 rounded-[26px] border border-slate-200 bg-slate-50 p-5">
              <p className="font-semibold text-ink">Mapa base</p>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Nombre</label>
                  <input value={mapForm.name} onChange={(event) => setMapForm({ ...mapForm, name: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Piso / zona</label>
                  <input value={mapForm.floor_zone} onChange={(event) => setMapForm({ ...mapForm, floor_zone: event.target.value })} />
                </div>
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Background URL</label>
                  <input value={mapForm.background_url} onChange={(event) => setMapForm({ ...mapForm, background_url: event.target.value })} />
                </div>
                <div>
                  <label className="mb-2 block text-sm font-semibold text-slate-700">Overlay URL</label>
                  <input value={mapForm.overlay_url} onChange={(event) => setMapForm({ ...mapForm, overlay_url: event.target.value })} />
                </div>
              </div>
              <label className="flex items-center gap-3 rounded-[20px] border border-slate-200 bg-white px-4 py-4">
                <input
                  className="h-5 w-5"
                  type="checkbox"
                  checked={mapForm.is_active}
                  onChange={(event) => setMapForm({ ...mapForm, is_active: event.target.checked })}
                />
                <span className="text-sm font-semibold text-slate-700">Mapa activo</span>
              </label>
              <div className="flex flex-wrap gap-3">
                <button
                  className="rounded-full bg-ink px-5 py-3 text-sm font-semibold text-white"
                  type="button"
                  onClick={() =>
                    onCreateMap({
                      name: mapForm.name,
                      floor_zone: mapForm.floor_zone || null,
                      background_url: mapForm.background_url || null,
                      overlay_url: mapForm.overlay_url || null,
                      is_active: mapForm.is_active,
                    })
                  }
                >
                  Crear mapa
                </button>
                <button
                  className="rounded-full border border-slate-200 px-5 py-3 text-sm font-semibold text-slate-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedMap}
                  onClick={() => {
                    if (selectedMap) {
                      onUpdateMap(selectedMap.id, {
                        name: mapForm.name,
                        floor_zone: mapForm.floor_zone || null,
                        background_url: mapForm.background_url || null,
                        overlay_url: mapForm.overlay_url || null,
                        is_active: mapForm.is_active,
                      });
                    }
                  }}
                >
                  Guardar
                </button>
                <button
                  className="rounded-full border border-rose-200 px-5 py-3 text-sm font-semibold text-rose-700 disabled:opacity-40"
                  type="button"
                  disabled={!selectedMap}
                  onClick={() => {
                    if (selectedMap) {
                      onDeleteMap(selectedMap.id);
                    }
                  }}
                >
                  Eliminar
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
