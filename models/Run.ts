import mongoose, { Schema, Document } from 'mongoose';

export interface IRunStep {
    name: string;
    timestamp: Date;
    message?: string;
}

export interface IRun extends Document {
    runId: string;
    status: 'RUNNING' | 'SUCCESS' | 'FAILED';
    startTime: Date;
    endTime?: Date;
    durationMs?: number;
    steps: IRunStep[];
    metrics?: any;
    errorContext?: {
        type: string;
        message: string;
        stack?: string;
    };
}

const RunStepSchema = new Schema({
    name: { type: String, required: true },
    timestamp: { type: Date, default: Date.now },
    message: { type: String }
}, { _id: false });

const RunSchema: Schema = new Schema({
    runId: { type: String, required: true, unique: true },
    status: { type: String, enum: ['RUNNING', 'SUCCESS', 'FAILED'], default: 'RUNNING' },
    startTime: { type: Date, default: Date.now },
    endTime: { type: Date },
    durationMs: { type: Number },
    steps: [RunStepSchema],
    metrics: { type: Schema.Types.Mixed },
    errorContext: {
        type: { type: String },
        message: { type: String },
        stack: { type: String }
    }
}, { timestamps: true });

export const Run = mongoose.model<IRun>('Run', RunSchema);
