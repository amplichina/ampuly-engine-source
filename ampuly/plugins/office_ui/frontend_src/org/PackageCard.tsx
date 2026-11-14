import { OrgIcon } from './OrgIcon'
import type { InstalledPackageInfo } from '../types/visual'
import { useI18n } from '../i18n'

interface PackageCardProps {
  pkg: InstalledPackageInfo
  onUninstall?: (packageId: string) => void
  uninstallingId?: string | null
}

export function PackageCard({ pkg, onUninstall, uninstallingId }: PackageCardProps) {
  const { t } = useI18n()
  const isUninstalling = uninstallingId === pkg.package_id

  return (
    <div className="pkg-card">
      <div className="pkg-card-header">
        <OrgIcon name="package" size={18} className="pkg-card-icon" />
        <div className="pkg-card-title-wrap">
          <span className="pkg-card-name">{pkg.name || pkg.package_id}</span>
          <span className="pkg-card-version">v{pkg.version}</span>
        </div>
      </div>

      <div className="pkg-card-stats">
        <span className="pkg-card-stat">
          <OrgIcon name="team" size={13} className="pkg-stat-icon" />
          {t('arch.rolesCount', { count: pkg.role_ids.length })}
        </span>
        <span className="pkg-card-stat">
          <OrgIcon name="templates" size={13} className="pkg-stat-icon" />
          {t('arch.templatesCount', { count: pkg.template_ids.length })}
        </span>
      </div>

      {pkg.installed_at && (
        <div className="pkg-card-date">
          {t('arch.installedOn', { date: new Date(pkg.installed_at).toLocaleDateString() })}
        </div>
      )}

      {onUninstall && (
        <div className="pkg-card-actions">
          <button
            className="pkg-btn pkg-btn-danger"
            disabled={isUninstalling}
            onClick={(e) => { e.stopPropagation(); onUninstall(pkg.package_id) }}
          >
            {isUninstalling ? t('arch.removing') : t('arch.uninstall')}
          </button>
        </div>
      )}
    </div>
  )
}
