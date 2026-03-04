import mongoose, { Schema, Document } from 'mongoose';

// --- Tipos ---
export type Modalidad = 'presencial' | 'teleformacion' | 'mixta';

export interface IOcupacion {
    codigo: string;
    descripcion: string;
}

export interface IEspecificacionFormativa extends Document {
    codigo: string;
    denominacion: string;
    version: number;
    familia_profesional: string;
    area_profesional: string;
    competencia_transversal: string | null;
    nivel_cualificacion: number | null;
    modalidad_imparticion: Modalidad;
    duracion_total: number;
    duracion_total_parte_presencial: number | null;
    ocupaciones_relacionadas: IOcupacion[];
}

// --- Sub-esquema de Ocupación ---
const OcupacionSchema = new Schema({
    codigo: { type: String, default: "" },
    descripcion: { type: String, default: "" }
}, { _id: false });

// --- Esquema Principal ---
const EspecificacionFormativaSchema: Schema = new Schema({
    codigo: { type: String, required: true, unique: true },
    denominacion: { type: String, default: "" },
    version: { type: Number, default: 0 },
    familia_profesional: { type: String, default: "" },
    area_profesional: { type: String, default: "" },
    competencia_transversal: { type: String, default: null },
    nivel_cualificacion: { type: Number, default: null },
    modalidad_imparticion: {
        type: String,
        enum: ['presencial', 'teleformacion', 'mixta'],
        required: true
    },
    duracion_total: { type: Number, default: 0 },
    duracion_total_parte_presencial: { type: Number, default: null },
    ocupaciones_relacionadas: { type: [OcupacionSchema], default: [] }
}, { timestamps: false, versionKey: false });

export const EspecificacionFormativa = mongoose.model<IEspecificacionFormativa>(
    'EspecificacionFormativa',
    EspecificacionFormativaSchema,
    'especificacion_formativa'
);
