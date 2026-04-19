import i18n from 'i18next'
import type { Resource } from 'i18next'
import { initReactI18next } from 'react-i18next'
import { useSettingsStore } from '@/stores/settings'

type SupportedLanguage = 'en' | 'zh' | 'fr' | 'ar' | 'zh_TW' | 'ru' | 'ja' | 'de' | 'uk' | 'ko' | 'vi'
type TranslationDictionary = Record<string, unknown>

// Simplified-Chinese is the product default (首次进入即中文); English
// stays as the i18next fallback so any translation key missing from
// zh.json silently resolves to its English string instead of showing
// the raw key to the user.
const defaultLanguage: SupportedLanguage = 'zh'
const fallbackLanguage: SupportedLanguage = 'en'
const supportedLanguages: SupportedLanguage[] = [
  'en',
  'zh',
  'fr',
  'ar',
  'zh_TW',
  'ru',
  'ja',
  'de',
  'uk',
  'ko',
  'vi',
]

const localeModules = import.meta.glob('./locales/*.json', {
  import: 'default',
}) as Record<string, () => Promise<TranslationDictionary>>

const languageLoaders = supportedLanguages.reduce<Record<SupportedLanguage, () => Promise<TranslationDictionary>>>(
  (accumulator, language) => {
    const loader = localeModules[`./locales/${language}.json`]

    if (!loader) {
      throw new Error(`Missing locale loader for language '${language}'.`)
    }

    accumulator[language] = loader
    return accumulator
  },
  {} as Record<SupportedLanguage, () => Promise<TranslationDictionary>>
)

const isSupportedLanguage = (language: string): language is SupportedLanguage =>
  supportedLanguages.includes(language as SupportedLanguage)

const getStoredLanguage = () => {
  try {
    const settingsString = localStorage.getItem('settings-storage')
    if (settingsString) {
      const settings = JSON.parse(settingsString)
      const storedLanguage = settings.state?.language
      if (typeof storedLanguage === 'string' && isSupportedLanguage(storedLanguage)) {
        return storedLanguage
      }
    }
  } catch (e) {
    console.error('Failed to get stored language:', e)
  }
  return defaultLanguage
}

const loadTranslationDictionary = async (language: SupportedLanguage): Promise<TranslationDictionary> => {
  return languageLoaders[language]()
}

const buildInitialResources = async (languages: SupportedLanguage[]): Promise<Resource> => {
  const uniqueLanguages = Array.from(new Set([fallbackLanguage, ...languages]))
  const resourceEntries = await Promise.all(
    uniqueLanguages.map(async (language) => {
      const translation = await loadTranslationDictionary(language)
      return [language, { translation }] as const
    })
  )

  return Object.fromEntries(resourceEntries)
}

const ensureLanguageLoaded = async (language: SupportedLanguage) => {
  if (i18n.hasResourceBundle(language, 'translation')) {
    return
  }

  const translation = await loadTranslationDictionary(language)
  i18n.addResourceBundle(language, 'translation', translation, true, true)
}

const syncLanguage = async (language: SupportedLanguage) => {
  await ensureLanguageLoaded(language)

  if (i18n.language !== language) {
    await i18n.changeLanguage(language)
  }
}

let initializationPromise: Promise<typeof i18n> | null = null

export const initializeI18n = () => {
  if (initializationPromise) {
    return initializationPromise
  }

  initializationPromise = (async () => {
    const initialLanguage = getStoredLanguage()
    const resources = await buildInitialResources([initialLanguage])

    await i18n.use(initReactI18next).init({
      resources,
      lng: initialLanguage,
      fallbackLng: fallbackLanguage,
      interpolation: {
        escapeValue: false,
      },
      // Configuration to handle missing translations
      returnEmptyString: false,
      returnNull: false,
    })

    useSettingsStore.subscribe((state) => {
      void syncLanguage(state.language)
    })

    return i18n
  })().catch((error) => {
    initializationPromise = null
    throw error
  })

  return initializationPromise
}

void initializeI18n()

export default i18n
