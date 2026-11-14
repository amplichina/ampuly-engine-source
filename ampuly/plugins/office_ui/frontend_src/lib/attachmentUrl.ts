// Where a stored attachment is served (data phase 0, 2026-09-22). An attachment lives in its own project's folder
// (projects/<project>/attachments/<id>/<name>, which its disk_path says); the three-part route names that project. A
// ref without a project, or of the default project, keeps the two-part route every earlier link used. Pure.

type AttachmentLike = { attachment_id: string; filename: string; disk_path?: string }

/** The project an attachment belongs to, read from its disk path ('' when it does not say). */
export function attachmentProject(ref: AttachmentLike): string {
  const match = /^projects\/([^/]+)\/attachments\//.exec(String(ref.disk_path ?? '').replace(/\\/g, '/'))
  return match?.[1] ?? ''
}

export function attachmentUrl(ref: AttachmentLike): string {
  const project = attachmentProject(ref)
  const tail = `${encodeURIComponent(ref.attachment_id)}/${encodeURIComponent(ref.filename)}`
  return project && project !== 'default' ? `/api/attachments/${encodeURIComponent(project)}/${tail}` : `/api/attachments/${tail}`
}

/** The same attachment, asked for as a download: the server hands it over instead of opening it in a tab, and
 *  records that it was downloaded (data phase 2, D7). */
export function attachmentDownloadUrl(ref: AttachmentLike): string {
  return `${attachmentUrl(ref)}?download=1`
}
