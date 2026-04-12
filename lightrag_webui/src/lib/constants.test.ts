import { describe, expect, it } from 'bun:test'

import {
  defaultMaxUploadSize,
  supportedFileTypes,
  supportedFileTypesDescription
} from './constants'

describe('supported upload file types', () => {
  it('includes common image formats for direct multimodal upload', () => {
    expect(supportedFileTypes['image/png']).toEqual(['.png'])
    expect(supportedFileTypes['image/jpeg']).toEqual(['.jpg', '.jpeg'])
    expect(supportedFileTypes['image/webp']).toEqual(['.webp'])
    expect(supportedFileTypes['image/gif']).toEqual(['.gif'])
    expect(supportedFileTypes['image/bmp']).toEqual(['.bmp'])
  })

  it('uses the backend-aligned 100MB default upload limit when no env override is set', () => {
    expect(defaultMaxUploadSize).toBe(100 * 1024 * 1024)
  })

  it('builds a human-readable supported types description from the accept map', () => {
    expect(supportedFileTypesDescription).toContain('PNG')
    expect(supportedFileTypesDescription).toContain('JPG')
    expect(supportedFileTypesDescription).toContain('PDF')
  })
})
