pipeline {
  agent any
  options { skipDefaultCheckout(true) }

  environment {
    ESP_IDF_IMAGE = 'docker.io/espressif/idf:v5.5.2'
  }

  stages {
    stage('Checkout') {
      steps {
        checkout scm
      }
    }

    stage('ESP-IDF Build (Windows + Podman)') {
      steps {
        powershell '''
          $ErrorActionPreference = "Stop"

          # Verify podman is available
          podman --version

          # Run the ESP-IDF container, mount the workspace, and build
          podman run --rm `
            -v "$PWD:/project" `
            -w /project `
            ${env:ESP_IDF_IMAGE} `
            bash -lc "source /opt/esp/idf/export.sh && idf.py --version && idf.py set-target esp32 && idf.py build"
        '''
      }
    }

    stage('Archive Artifacts') {
      steps {
        archiveArtifacts artifacts: 'build/**', fingerprint: true
      }
    }
  }

  post {
    success { echo '✅ ESP-IDF build succeeded' }
    failure { echo '❌ ESP-IDF build failed' }
  }
}