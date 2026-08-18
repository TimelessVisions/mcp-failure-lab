/**
 * Types for the generated validator.
 *
 * The validator is emitted by `scripts/compile-schema.mjs` from the same schema
 * the Python exporter generates, so this declaration describes a shape nobody
 * maintains by hand — only the signature.
 */
import type { ErrorObject } from 'ajv';

export interface CompiledValidator {
  (data: unknown): boolean;
  errors?: ErrorObject[] | null;
}

declare const validate: CompiledValidator;
export default validate;
