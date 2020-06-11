//! Application settings

use config::{Config, ConfigError, Environment, File};
use serde::Deserialize;
use url::Url;

const DEFAULT_PORT: u16 = 8000;
const ENV_PREFIX: &str = "autoend_";

#[derive(Clone, Debug, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub debug: bool,
    pub port: u16,
    pub host: String,
    pub database_url: String,
    pub database_pool_max_size: Option<u32>,
    #[cfg(any(test, feature = "db_test"))]
    pub database_use_test_transactions: bool,

    pub human_logs: bool,

    pub statsd_host: Option<String>,
    pub statsd_port: u16,
    pub statsd_label: String,
}

impl Default for Settings {
    fn default() -> Settings {
        Settings {
            debug: false,
            port: DEFAULT_PORT,
            host: "127.0.0.1".to_string(),
            database_url: "mysql://root@127.0.0.1/autopush".to_string(),
            database_pool_max_size: None,
            #[cfg(any(test, feature = "db_test"))]
            database_use_test_transactions: false,
            statsd_host: None,
            statsd_port: 8125,
            statsd_label: "autoendpoint".to_string(),
            human_logs: false,
        }
    }
}

impl Settings {
    /// Load the settings from the config file if supplied, then the environment.
    pub fn with_env_and_config_file(filename: &Option<String>) -> Result<Self, ConfigError> {
        let mut config = Config::new();

        // Merge the config file if supplied
        if let Some(config_filename) = filename {
            config.merge(File::with_name(config_filename))?;
        }

        // Merge the environment overrides
        config.merge(Environment::with_prefix(ENV_PREFIX))?;

        config.try_into::<Self>().or_else(|error| match error {
            // Configuration errors are not very sysop friendly, Try to make them
            // a bit more 3AM useful.
            ConfigError::Message(error_msg) => {
                println!("Bad configuration: {:?}", &error_msg);
                println!("Please set in config file or use environment variable.");
                println!(
                    "For example to set `database_url` use env var `{}_DATABASE_URL`\n",
                    ENV_PREFIX.to_uppercase()
                );
                error!("Configuration error: Value undefined {:?}", &error_msg);
                Err(ConfigError::NotFound(error_msg))
            }
            _ => {
                error!("Configuration error: Other: {:?}", &error);
                Err(error)
            }
        })
    }

    /// A simple banner for display of certain settings at startup
    pub fn banner(&self) -> String {
        let db = Url::parse(&self.database_url)
            .map(|url| url.scheme().to_owned())
            .unwrap_or_else(|_| "<invalid db>".to_owned());
        format!("http://{}:{} ({})", self.host, self.port, db)
    }
}