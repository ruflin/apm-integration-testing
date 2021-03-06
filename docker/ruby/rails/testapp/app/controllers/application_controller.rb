class ApplicationController < ActionController::API
  def healthcheck 
    render json: 'ok'
  end

  def foo
    render json: "foo"
  end

  def bar
    render json: bar_span()
  end

  private

  def bar_span
    extra_span()
    "bar"
  end

  def extra_span
    ElasticAPM.span 'app.extra' do
      "extra"
    end
  end
end
